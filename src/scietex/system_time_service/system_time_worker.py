"""The system time worker.

Owns the clock runtime, applies delivered settings, and publishes telemetry.
Settings application is deliberately non-fatal: a raising apply hook would fail
worker startup and lose the heartbeat, so every failure is captured into
telemetry instead.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import msgspec
from glide import ConnectionError as GlideConnectionError
from glide import ExpirySet, ExpiryType, RequestError
from glide import TimeoutError as GlideTimeoutError
from scietex.service import Manager, ValkeyWorker, ValkeyWorkerConfig

from .clock import commands
from .clock.battery import resolve_provider
from .clock.capabilities import detect_capabilities
from .clock.platform import detect_platform
from .clock.runner import (
    DRY_RUN,
    CommandResult,
    DryRunCommandRunner,
    RealCommandRunner,
)
from .config import (
    SYSTEM_TIME_SECTION,
    SYSTEM_TIME_SETTINGS_DEFAULTS,
    SystemTimeSettings,
    read_system_time_config,
)
from .runtime import ClockRuntime, ClockSnapshot
from .task_handlers import (
    APPLY_TASK,
    READ_RTC_TASK,
    SET_CLOCK_TASK,
    STATUS_TASK,
    ApplyHandler,
    ReadRtcHandler,
    SetClockHandler,
    StatusHandler,
)
from .telemetry import TELEMETRY_INTERVAL, TELEMETRY_TTL, build_telemetry

#: Commands the dry-run runner executes for real. Everything else is recorded
#: and skipped, so a dry run exercises the full path without touching the host.
READ_ONLY_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("timedatectl", "show"),
    ("timedatectl", "show-timesync"),
    ("chronyc", "-c", "tracking"),
    ("chronyc", "-c", "sources"),
    ("systemctl", "is-active"),
)

#: The interval is remote, so the loop re-reads it every tick instead of
#: sleeping the whole interval; otherwise shrinking 24 h -> 5 min would take
#: 24 h to take effect. TICK also bounds how long a disable takes to apply.
RTC_SYNC_TICK: float = 5.0

#: sysfs reports the RTC at one-second resolution, so anything within a few
#: seconds is measurement noise; rewriting it would be pure churn.
RTC_SYNC_TOLERANCE_SECONDS: float = 5.0


def apply_settings(runtime: ClockRuntime) -> CommandResult:
    """Converge the host onto the runtime's settings.

    Only the differences are applied, so a restart with unchanged settings runs
    no commands. The first failure stops the sequence and is returned.
    """
    settings = runtime.settings
    caps = runtime.capabilities
    snapshot = runtime.collect()

    if settings.timezone and snapshot.clock.timezone != settings.timezone:
        result = commands.set_timezone(runtime.runner, caps, settings.timezone)
        if not result.ok:
            return result

    if caps.ntp_configurable:
        result = commands.write_ntp_servers(
            runtime.runner, caps, runtime.platform_info, settings.ntp_servers
        )
        if not result.ok:
            return result

    want_ntp = settings.clock_source == "auto"
    if caps.ntp_controllable and snapshot.clock.ntp_enabled != want_ntp:
        result = commands.set_ntp_enabled(runtime.runner, caps, want_ntp)
        if not result.ok:
            return result

    if snapshot.clock.rtc_in_local_tz != settings.rtc_in_local_tz:
        result = commands.set_rtc_in_local_tz(
            runtime.runner, caps, settings.rtc_in_local_tz
        )
        if not result.ok:
            return result

    return CommandResult(ok=True)


class RtcSyncOutcome(msgspec.Struct, frozen=True):
    """Result of one sync attempt. ``attempted`` is False for a deliberate skip."""

    attempted: bool
    written: bool = False
    error: str | None = None


def rtc_sync_offset(snapshot: ClockSnapshot) -> float | None:
    """RTC minus system clock, corrected for a local-time RTC.

    sysfs ``since_epoch`` interprets the RTC registers as UTC regardless of
    systemd's LocalRTC setting, so a local-time RTC reports the UTC offset as
    drift.
    """
    if snapshot.rtc.drift_seconds is None:
        return None
    expected = (
        float(snapshot.clock.utc_offset_seconds or 0)
        if snapshot.clock.rtc_in_local_tz
        else 0.0
    )
    return snapshot.rtc.drift_seconds - expected


def sync_rtc(runtime: ClockRuntime) -> RtcSyncOutcome:
    """Copy the system clock into the RTC if it is worth doing. Never raises."""
    settings = runtime.settings
    if settings.rtc_sync_interval_seconds is None:
        return RtcSyncOutcome(attempted=False)

    caps = runtime.capabilities
    if not caps.rtc_writable:
        # No error: an unprivileged or RTC-less host would otherwise carry a
        # permanent red error for a feature it can never use. The UI gates the
        # control on ``rtc_writable``.
        return RtcSyncOutcome(attempted=False)

    snapshot = runtime.collect()

    if settings.clock_source == "auto" and not snapshot.ntp.synchronized:
        # While NTP is still converging the system clock may be the wrong one,
        # and writing it would destroy a correct RTC. This is the one place a
        # sync can do harm.
        return RtcSyncOutcome(attempted=False)

    offset = rtc_sync_offset(snapshot)
    if offset is not None and abs(offset) <= RTC_SYNC_TOLERANCE_SECONDS:
        # A confirmed-in-sync pass counts as a successful sync: the RTC holds
        # the right time, which is what the interval promises.
        runtime.last_rtc_sync_at = datetime.now(timezone.utc)
        runtime.last_rtc_sync_error = None
        return RtcSyncOutcome(attempted=False, written=False)

    result = commands.write_rtc_from_system(
        runtime.runner, caps, in_local_tz=settings.rtc_in_local_tz
    )
    if result.ok:
        runtime.last_rtc_sync_at = datetime.now(timezone.utc)
        runtime.last_rtc_sync_error = DRY_RUN if result.error_code == DRY_RUN else None
        return RtcSyncOutcome(attempted=True, written=True)

    # ``last_rtc_sync_at`` keeps its previous value: it means "last time it
    # worked", not "last time it ran".
    runtime.last_rtc_sync_error = result.stderr or result.error_code
    return RtcSyncOutcome(
        attempted=True, written=False, error=runtime.last_rtc_sync_error
    )


class SystemTimeWorker(ValkeyWorker):
    """Valkey worker that owns the host clock."""

    def __init__(
        self,
        config: ValkeyWorkerConfig | None = None,
        *,
        client_factory=None,
        theme=None,
        dry_run: bool = False,
    ) -> None:
        super().__init__(config, client_factory=client_factory, theme=theme)
        self._dry_run = dry_run
        self._runtime: ClockRuntime | None = None
        self._telemetry_encoder = msgspec.msgpack.Encoder()
        self._telemetry_key = (
            f"scietex:{self.service_name}:{self.instance_id}:telemetry"
        )
        self._startup_complete = False
        self._rtc_sync_due: float | None = None

        self.register_config_settings(
            SYSTEM_TIME_SECTION,
            SystemTimeSettings,
            apply=self._apply_settings,
            defaults=SYSTEM_TIME_SETTINGS_DEFAULTS,
            bootstrap=lambda: read_system_time_config(self.conf_dir),
        )

    @property
    def runtime(self) -> ClockRuntime:
        if self._runtime is None:
            raise RuntimeError("worker is not initialized")
        return self._runtime

    def _build_runtime(self, settings: SystemTimeSettings) -> ClockRuntime:
        runner = (
            DryRunCommandRunner(
                RealCommandRunner(), read_only_prefixes=READ_ONLY_PREFIXES
            )
            if self._dry_run
            else RealCommandRunner()
        )
        platform_info = detect_platform()
        battery_provider = resolve_provider(
            provider=settings.battery_provider,
            platform_info=platform_info,
            battery_command=settings.battery_command,
            battery_command_timeout=settings.battery_command_timeout,
        )
        capabilities = detect_capabilities(
            runner,
            platform_info,
            rtc_sysfs=settings.rtc_sysfs,
            battery_available=battery_provider.available(),
        )
        return ClockRuntime(
            runner=runner,
            platform_info=platform_info,
            battery_provider=battery_provider,
            settings=settings,
            capabilities=capabilities,
        )

    async def initialize(self) -> bool:
        if not await super().initialize():
            return False

        settings = self.current_config_settings(SYSTEM_TIME_SECTION)
        if not isinstance(settings, SystemTimeSettings):
            settings = SYSTEM_TIME_SETTINGS_DEFAULTS

        self._runtime = self._build_runtime(settings)
        self._runtime.collect()

        self.add_task_handler(StatusHandler, runtime=self._runtime)
        self.add_task_handler(ReadRtcHandler, runtime=self._runtime)
        self.add_task_handler(ApplyHandler, runtime=self._runtime)
        self.add_task_handler(SetClockHandler, runtime=self._runtime)

        result = apply_settings(self._runtime)
        self._runtime.pending_apply = not result.ok
        self._runtime.last_error = (
            None if result.ok else (result.stderr or result.error_code)
        )
        if not result.ok:
            self.logger.warning(
                "Initial settings apply failed: %s", self._runtime.last_error
            )

        self._startup_complete = True
        return True

    def _apply_settings(self, settings: SystemTimeSettings) -> None:
        """Config-apply hook. Never raises for a host failure."""
        if self._runtime is None:
            return
        self._runtime.settings = settings
        if not self._startup_complete:
            return
        result = apply_settings(self._runtime)
        self._runtime.pending_apply = not result.ok
        self._runtime.last_error = (
            None if result.ok else (result.stderr or result.error_code)
        )
        if not result.ok:
            self.logger.warning("Settings apply failed: %s", self._runtime.last_error)

    @Manager(name="Telemetry")
    async def telemetry_loop(self) -> None:
        """Publish the cached snapshot every interval, re-reading on the slower cadence."""
        client = self.client
        if client is not None and self._runtime is not None:
            try:
                snapshot = self._runtime.collect()
                telemetry = build_telemetry(
                    service=self.service_name,
                    instance_id=self.instance_id,
                    clock=snapshot.clock,
                    ntp=snapshot.ntp,
                    rtc=snapshot.rtc,
                    battery=snapshot.battery,
                    capabilities=self._runtime.capabilities,
                    clock_source=self._runtime.settings.clock_source,
                    pending_apply=self._runtime.pending_apply,
                    last_error=self._runtime.last_error,
                    last_rtc_sync_at=self._runtime.last_rtc_sync_at,
                    last_rtc_sync_error=self._runtime.last_rtc_sync_error,
                )
                await client.set(
                    self._telemetry_key,
                    value=self._telemetry_encoder.encode(telemetry),
                    expiry=ExpirySet(ExpiryType.SEC, TELEMETRY_TTL),
                )
            except (GlideConnectionError, RequestError, GlideTimeoutError) as exc:
                self.logger.log(
                    logging.WARNING, "Failed to publish system time telemetry: %s", exc
                )
        await asyncio.sleep(TELEMETRY_INTERVAL)

    @Manager(name="RtcSync")
    async def rtc_sync_loop(self) -> None:
        """Write the system clock into the RTC on the configured interval."""
        while True:
            runtime = self._runtime
            if runtime is None:
                await asyncio.sleep(RTC_SYNC_TICK)
                continue

            interval = runtime.settings.rtc_sync_interval_seconds
            if interval is None:
                self._rtc_sync_due = None
                await asyncio.sleep(RTC_SYNC_TICK)
                continue

            now = time.monotonic()
            if self._rtc_sync_due is None or self._rtc_sync_due - now > interval:
                # First sync waits a full interval, so startup never writes on
                # its own; a shrunken interval re-clamps here instead of
                # waiting out the old one.
                self._rtc_sync_due = now + interval

            wait = self._rtc_sync_due - now
            if wait <= 0:
                self._rtc_sync_due = now + interval
                try:
                    sync_rtc(runtime)
                except Exception as exc:  # noqa: BLE001 - keeps the manager alive
                    # A raise would burn the manager's retry budget; commands
                    # never raises, but ``collect`` touches sysfs and zoneinfo.
                    runtime.last_rtc_sync_error = str(exc)
                    self.logger.warning("RTC sync failed: %s", exc)
                continue

            await asyncio.sleep(min(wait, RTC_SYNC_TICK))


__all__ = [
    "APPLY_TASK",
    "READ_RTC_TASK",
    "SET_CLOCK_TASK",
    "STATUS_TASK",
    "RtcSyncOutcome",
    "SystemTimeWorker",
    "apply_settings",
    "rtc_sync_offset",
    "sync_rtc",
]
