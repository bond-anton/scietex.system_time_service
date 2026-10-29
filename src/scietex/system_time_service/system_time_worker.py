"""The system time worker.

Owns the clock runtime, applies delivered settings, and publishes telemetry.
Settings application is deliberately non-fatal: a raising apply hook would fail
worker startup and lose the heartbeat, so every failure is captured into
telemetry instead.
"""

import asyncio
import logging

import msgspec
from glide import ConnectionError as GlideConnectionError
from glide import ExpirySet, ExpiryType, RequestError
from glide import TimeoutError as GlideTimeoutError
from scietex.service import Manager, ValkeyWorker, ValkeyWorkerConfig

from .clock import commands
from .clock.battery import resolve_provider
from .clock.capabilities import detect_capabilities
from .clock.platform import detect_platform
from .clock.runner import CommandResult, DryRunCommandRunner, RealCommandRunner
from .config import (
    SYSTEM_TIME_SECTION,
    SYSTEM_TIME_SETTINGS_DEFAULTS,
    SystemTimeSettings,
    read_system_time_config,
)
from .runtime import ClockRuntime
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


__all__ = [
    "APPLY_TASK",
    "READ_RTC_TASK",
    "SET_CLOCK_TASK",
    "STATUS_TASK",
    "SystemTimeWorker",
    "apply_settings",
]
