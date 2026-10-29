"""Capability detection.

Capabilities are derived from the platform, the available binaries, the active
NTP daemon, and the process privilege level. The API and the UI read these to
decide what to offer; the command layer re-checks them before mutating.
"""

import os
from pathlib import Path

import msgspec

from .platform import (
    PlatformInfo,
    detect_ntp_daemon,
    has_binary,
    has_cap_sys_time,
    is_privileged,
)


class Capabilities(msgspec.Struct, frozen=True):
    """What this host and this process can actually do."""

    timedatectl: bool
    timezone_settable: bool
    clock_settable: bool
    ntp_controllable: bool
    ntp_configurable: bool
    rtc_present: bool
    rtc_writable: bool
    battery_available: bool
    privileged: bool
    platform_id: str
    ntp_daemon: str


def detect_capabilities(
    runner,
    platform_info: PlatformInfo,
    *,
    rtc_sysfs: str,
    rtc_device: str,
    battery_available: bool,
    privileged: bool | None = None,
    cap_sys_time: bool | None = None,
) -> Capabilities:
    """Probe the host and return its capability set.

    ``privileged`` and ``cap_sys_time`` are injectable so tests can exercise
    every branch without running as root or holding the capability.
    """
    root = is_privileged() if privileged is None else privileged
    timedatectl = has_binary("timedatectl")
    daemon = detect_ntp_daemon(runner)
    rtc_present = Path(rtc_sysfs, "since_epoch").exists()
    can_set_rtc = has_cap_sys_time() if cap_sys_time is None else cap_sys_time

    # euid is not enough: a rootless container runs as uid 0 while the device
    # node stays owned by an unmapped host group, so writes fail with EACCES.
    # File access is not enough either: RTC_SET_TIME is gated on CAP_SYS_TIME
    # in the initial user namespace, which a rootless container cannot hold.
    rtc_writable = (
        rtc_present and os.access(rtc_device, os.R_OK | os.W_OK) and can_set_rtc
    )

    # NTP servers can only be written where the platform has a drop-in
    # directory AND the running daemon reads one.
    ntp_configurable = (
        root
        and daemon in {"chrony", "timesyncd"}
        and (
            (daemon == "chrony" and platform_info.chrony_confdir is not None)
            or (daemon == "timesyncd" and platform_info.timesyncd_confdir is not None)
        )
    )

    return Capabilities(
        timedatectl=timedatectl,
        timezone_settable=timedatectl and root,
        clock_settable=timedatectl and root,
        ntp_controllable=timedatectl and daemon in {"chrony", "timesyncd"} and root,
        ntp_configurable=ntp_configurable,
        rtc_present=rtc_present,
        rtc_writable=rtc_writable,
        battery_available=battery_available,
        privileged=root,
        platform_id=platform_info.platform_id,
        ntp_daemon=daemon,
    )
