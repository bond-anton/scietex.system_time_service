"""Shared runtime state for the clock layer.

The worker owns one ``ClockRuntime`` and injects it into every task handler, so
handlers see the same runner, platform and capability snapshot the telemetry
loop uses. Handlers are re-instantiated on restart; the injected object is not.
"""

from datetime import datetime

import msgspec

from .clock.battery import BatteryStatus, RtcBatteryProvider
from .clock.capabilities import Capabilities
from .clock.platform import PlatformInfo
from .clock.runner import CommandRunner
from .clock.status import ClockStatus, NtpStatus, RtcStatus
from .config import SystemTimeSettings


class ClockSnapshot(msgspec.Struct, frozen=True):
    """One collected reading of the host clock state."""

    clock: ClockStatus
    ntp: NtpStatus
    rtc: RtcStatus
    battery: BatteryStatus


class ClockRuntime:
    """Mutable state shared by the worker and its task handlers.

    ``last_rtc_sync_at``/``last_rtc_sync_error`` are in-memory and reset on
    worker restart. Telemetry is live state, so a value that survives the
    process that produced it would be a lie.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner,
        platform_info: PlatformInfo,
        battery_provider: RtcBatteryProvider,
        settings: SystemTimeSettings,
        capabilities: Capabilities,
    ) -> None:
        self.runner = runner
        self.platform_info = platform_info
        self.battery_provider = battery_provider
        self.settings = settings
        self.capabilities = capabilities
        self.snapshot: ClockSnapshot | None = None
        self.pending_apply: bool = False
        self.last_error: str | None = None
        self.last_rtc_sync_at: datetime | None = None
        self.last_rtc_sync_error: str | None = None

    def collect(self) -> ClockSnapshot:
        """Read the host clock state and cache it."""
        from .clock.status import read_clock_status, read_ntp_status, read_rtc_status

        clock = read_clock_status(self.runner, rtc_sysfs=self.settings.rtc_sysfs)
        ntp = read_ntp_status(self.runner, self.capabilities.ntp_daemon)  # type: ignore[arg-type]
        rtc = read_rtc_status(
            self.runner,
            rtc_sysfs=self.settings.rtc_sysfs,
            rtc_device=self.settings.rtc_device,
            in_local_tz=clock.rtc_in_local_tz,
        )
        battery = self.battery_provider.read(self.runner)
        self.snapshot = ClockSnapshot(clock=clock, ntp=ntp, rtc=rtc, battery=battery)
        return self.snapshot

    def refresh_capabilities(self) -> None:
        """Re-probe capabilities after a change that could affect them."""
        from .clock.capabilities import detect_capabilities

        self.capabilities = detect_capabilities(
            self.runner,
            self.platform_info,
            rtc_sysfs=self.settings.rtc_sysfs,
            rtc_device=self.settings.rtc_device,
            battery_available=self.battery_provider.available(),
        )
