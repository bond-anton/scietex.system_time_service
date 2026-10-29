"""Clock, timezone, NTP and RTC operations."""

from .battery import BatteryStatus, RtcBatteryProvider, resolve_provider
from .capabilities import Capabilities, detect_capabilities
from .platform import PlatformInfo, detect_platform
from .runner import CommandResult, CommandRunner, DryRunCommandRunner, RealCommandRunner
from .status import (
    ClockStatus,
    NtpSource,
    NtpStatus,
    RtcStatus,
    read_clock_status,
    read_ntp_status,
    read_rtc_status,
)

__all__ = [
    "BatteryStatus",
    "Capabilities",
    "ClockStatus",
    "CommandResult",
    "CommandRunner",
    "DryRunCommandRunner",
    "NtpSource",
    "NtpStatus",
    "PlatformInfo",
    "RealCommandRunner",
    "RtcBatteryProvider",
    "RtcStatus",
    "detect_capabilities",
    "detect_platform",
    "read_clock_status",
    "read_ntp_status",
    "read_rtc_status",
    "resolve_provider",
]
