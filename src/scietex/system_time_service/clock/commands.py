"""Mutating clock operations.

Each function checks the capability it needs before running anything, so a
missing privilege or an unsupported platform produces a specific error code
instead of a confusing command failure. Nothing here raises: the caller maps
the returned ``CommandResult`` into a task result.
"""

from pathlib import Path
from zoneinfo import available_timezones

from .capabilities import Capabilities
from .platform import PlatformInfo
from .runner import (
    DRY_RUN,
    FAILED,
    MISSING_BINARY,
    PERMISSION_DENIED,
    CommandResult,
    CommandRunner,
)

INVALID_TIMEZONE = "INVALID_TIMEZONE"
NTP_UNAVAILABLE = "NTP_UNAVAILABLE"
RTC_UNAVAILABLE = "RTC_UNAVAILABLE"
UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
CLOCK_SOURCE_AUTO = "CLOCK_SOURCE_AUTO"

#: Marker written into every managed drop-in so the service only ever rewrites
#: files it owns.
MANAGED_MARKER = "# Managed by scietex.system_time_service. Do not edit by hand."
CHRONY_DROPIN_NAME = "scietex-ntp.conf"
TIMESYNCD_DROPIN_NAME = "scietex-ntp.conf"


def _denied() -> CommandResult:
    return CommandResult(
        ok=False, error_code=PERMISSION_DENIED, stderr="operation requires root"
    )


def set_timezone(
    runner: CommandRunner, caps: Capabilities, timezone: str
) -> CommandResult:
    """Set the system timezone to an IANA name."""
    if not caps.timezone_settable:
        return (
            _denied()
            if not caps.privileged
            else CommandResult(ok=False, error_code=MISSING_BINARY)
        )
    if timezone not in available_timezones():
        return CommandResult(
            ok=False,
            error_code=INVALID_TIMEZONE,
            stderr=f"unknown timezone: {timezone}",
        )
    return runner.run(["timedatectl", "set-timezone", timezone], timeout=10.0)


def set_ntp_enabled(
    runner: CommandRunner, caps: Capabilities, enabled: bool
) -> CommandResult:
    """Enable or disable NTP synchronisation."""
    if not caps.ntp_controllable:
        if not caps.privileged:
            return _denied()
        return CommandResult(
            ok=False, error_code=NTP_UNAVAILABLE, stderr="no controllable NTP daemon"
        )
    return runner.run(
        ["timedatectl", "set-ntp", "true" if enabled else "false"], timeout=10.0
    )


def write_ntp_servers(
    runner: CommandRunner,
    caps: Capabilities,
    platform_info: PlatformInfo,
    servers: list[str],
) -> CommandResult:
    """Write the NTP server list into the platform's drop-in directory.

    The drop-in is the only file touched. The platform's own configuration is
    never edited, so removing the drop-in restores the distro defaults.
    """
    if not caps.ntp_configurable:
        if not caps.privileged:
            return _denied()
        if (
            platform_info.chrony_confdir is None
            and platform_info.timesyncd_confdir is None
        ):
            return CommandResult(
                ok=False,
                error_code=UNSUPPORTED_PLATFORM,
                stderr=f"no NTP drop-in directory known for platform {platform_info.platform_id}",
            )
        if caps.ntp_daemon == "chrony" and platform_info.chrony_confdir is None:
            return CommandResult(
                ok=False,
                error_code=UNSUPPORTED_PLATFORM,
                stderr=(
                    f"chrony confdir is not supported on "
                    f"{platform_info.platform_id} {platform_info.os_version}"
                ),
            )
        return CommandResult(
            ok=False,
            error_code=NTP_UNAVAILABLE,
            stderr="NTP daemon is not controllable",
        )

    if caps.ntp_daemon == "chrony":
        directory = Path(platform_info.chrony_confdir or "")
        body = "\n".join(f"server {server} iburst" for server in servers)
        target = directory / CHRONY_DROPIN_NAME
        restart_unit = "chronyd"
    else:
        directory = Path(platform_info.timesyncd_confdir or "")
        body = "[Time]\n" + (f"NTP={' '.join(servers)}\n" if servers else "")
        target = directory / TIMESYNCD_DROPIN_NAME
        restart_unit = "systemd-timesyncd"

    try:
        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{MANAGED_MARKER}\n{body}\n", encoding="utf-8")
    except PermissionError:
        return _denied()
    except OSError as exc:
        return CommandResult(ok=False, error_code=FAILED, stderr=str(exc))

    return runner.run(["systemctl", "restart", restart_unit], timeout=30.0)


def set_clock(
    runner: CommandRunner,
    caps: Capabilities,
    epoch_seconds: float,
    *,
    write_rtc: bool,
    in_local_tz: bool = False,
) -> CommandResult:
    """Set the system clock to an absolute instant.

    NTP is disabled first: setting the clock while a daemon is slewing it
    produces a fight the operator cannot see.
    """
    if not caps.clock_settable:
        return (
            _denied()
            if not caps.privileged
            else CommandResult(ok=False, error_code=MISSING_BINARY)
        )

    disabled = runner.run(["timedatectl", "set-ntp", "false"], timeout=10.0)
    if not disabled.ok:
        return disabled

    applied = runner.run(
        ["timedatectl", "set-time", f"@{epoch_seconds:.6f}"], timeout=10.0
    )
    if not applied.ok:
        return applied

    if write_rtc:
        return write_rtc_from_system(runner, caps, in_local_tz=in_local_tz)
    return applied


def write_rtc_from_system(
    runner: CommandRunner, caps: Capabilities, *, in_local_tz: bool = False
) -> CommandResult:
    """Copy the system clock into the RTC.

    The ``--localtime``/``--utc`` flag has to match what
    ``timedatectl set-local-rtc`` told systemd. ``hwclock`` writes the system
    time into the RTC registers interpreted through that flag, so a mismatch
    stores a time the boot path re-reads shifted by the UTC offset.
    """
    if not caps.rtc_present:
        return CommandResult(
            ok=False, error_code=RTC_UNAVAILABLE, stderr="no RTC on this host"
        )
    if not caps.privileged:
        return _denied()
    flag = "--localtime" if in_local_tz else "--utc"
    return runner.run(["hwclock", "--systohc", flag], timeout=10.0)


def set_rtc_in_local_tz(
    runner: CommandRunner, caps: Capabilities, enabled: bool
) -> CommandResult:
    """Tell systemd whether the RTC stores local time."""
    if not caps.privileged:
        return _denied()
    return runner.run(
        ["timedatectl", "set-local-rtc", "1" if enabled else "0"], timeout=10.0
    )


def is_dry_run(result: CommandResult) -> bool:
    return result.error_code == DRY_RUN
