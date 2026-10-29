"""Platform detection.

The service never guesses at a host's layout. It reads ``/etc/os-release`` and
the device tree, maps them to a known platform, and reports the platform's
capabilities. An unrecognised host is reported as unsupported rather than
being driven with commands that might be wrong for it.
"""

import os
import platform
import shutil
from pathlib import Path
from typing import Literal

import msgspec

PlatformId = Literal["fedora", "rhel", "debian", "ubuntu", "raspberry-pi-os", "unknown"]
NtpDaemon = Literal["chrony", "timesyncd", "ntpd", "none"]

OS_RELEASE_PATH = Path("/etc/os-release")
DEVICE_TREE_MODEL_PATH = Path("/proc/device-tree/model")

#: Minimum Ubuntu release whose chrony packaging ships ``confdir`` support.
UBUNTU_MIN_YEAR = 24


class PlatformInfo(msgspec.Struct, frozen=True):
    """What the host is, and where its NTP configuration lives."""

    platform_id: PlatformId
    os_name: str
    os_version: str
    machine: str
    is_raspberry_pi: bool

    #: Directory chrony includes ``*.conf`` files from, or ``None`` when the
    #: platform has no supported drop-in mechanism.
    chrony_confdir: str | None
    #: Directory systemd-timesyncd reads ``*.conf`` drop-ins from.
    timesyncd_confdir: str | None
    #: Whether the platform's chrony supports ``confdir`` at all.
    chrony_confdir_supported: bool


def _read_os_release(path: Path = OS_RELEASE_PATH) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _read_device_model(path: Path = DEVICE_TREE_MODEL_PATH) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip("\x00").strip()


def _ubuntu_year(version: str) -> int | None:
    """Extract the two-digit year from an Ubuntu version like ``24.04``."""
    head = version.split(".")[0]
    return int(head) if head.isdigit() else None


def detect_platform(
    *,
    os_release_path: Path = OS_RELEASE_PATH,
    device_model_path: Path = DEVICE_TREE_MODEL_PATH,
    machine: str | None = None,
) -> PlatformInfo:
    """Map the host to a known platform and its NTP configuration layout."""
    release = _read_os_release(os_release_path)
    os_id = release.get("ID", "").lower()
    os_like = release.get("ID_LIKE", "").lower().split()
    os_name = release.get("NAME", "unknown")
    os_version = release.get("VERSION_ID", "")
    arch = machine or platform.machine()
    model = _read_device_model(device_model_path)
    is_pi = "raspberry pi" in model.lower()

    if is_pi:
        return PlatformInfo(
            platform_id="raspberry-pi-os",
            os_name=os_name,
            os_version=os_version,
            machine=arch,
            is_raspberry_pi=True,
            chrony_confdir="/etc/chrony/conf.d",
            timesyncd_confdir="/etc/systemd/timesyncd.conf.d",
            chrony_confdir_supported=True,
        )

    if os_id == "fedora":
        return PlatformInfo(
            platform_id="fedora",
            os_name=os_name,
            os_version=os_version,
            machine=arch,
            is_raspberry_pi=False,
            chrony_confdir="/etc/chrony.d",
            timesyncd_confdir="/etc/systemd/timesyncd.conf.d",
            chrony_confdir_supported=True,
        )

    if os_id in {"rhel", "centos", "rocky", "almalinux"} or "rhel" in os_like:
        return PlatformInfo(
            platform_id="rhel",
            os_name=os_name,
            os_version=os_version,
            machine=arch,
            is_raspberry_pi=False,
            chrony_confdir="/etc/chrony.d",
            timesyncd_confdir="/etc/systemd/timesyncd.conf.d",
            chrony_confdir_supported=True,
        )

    if os_id == "ubuntu":
        year = _ubuntu_year(os_version)
        supported = year is not None and year >= UBUNTU_MIN_YEAR
        return PlatformInfo(
            platform_id="ubuntu",
            os_name=os_name,
            os_version=os_version,
            machine=arch,
            is_raspberry_pi=False,
            chrony_confdir="/etc/chrony/conf.d" if supported else None,
            timesyncd_confdir="/etc/systemd/timesyncd.conf.d",
            chrony_confdir_supported=supported,
        )

    if os_id == "debian" or "debian" in os_like:
        return PlatformInfo(
            platform_id="debian",
            os_name=os_name,
            os_version=os_version,
            machine=arch,
            is_raspberry_pi=False,
            chrony_confdir="/etc/chrony/conf.d",
            timesyncd_confdir="/etc/systemd/timesyncd.conf.d",
            chrony_confdir_supported=True,
        )

    return PlatformInfo(
        platform_id="unknown",
        os_name=os_name,
        os_version=os_version,
        machine=arch,
        is_raspberry_pi=False,
        chrony_confdir=None,
        timesyncd_confdir=None,
        chrony_confdir_supported=False,
    )


def detect_ntp_daemon(runner) -> NtpDaemon:
    """Return the active NTP daemon, probing in priority order."""
    for daemon, unit in (
        ("chrony", "chronyd"),
        ("timesyncd", "systemd-timesyncd"),
        ("ntpd", "ntpd"),
    ):
        result = runner.run(["systemctl", "is-active", unit], timeout=3.0)
        if result.ok and result.stdout.strip() == "active":
            return daemon  # type: ignore[return-value]
    return "none"


def is_privileged() -> bool:
    """Whether the process can perform mutating clock operations."""
    return os.geteuid() == 0


#: ``CAP_SYS_TIME`` is bit 25 of the capability bitmask.
CAP_SYS_TIME = 25

PROC_STATUS_PATH = Path("/proc/self/status")


def has_cap_sys_time(status_path: Path = PROC_STATUS_PATH) -> bool:
    """Whether the effective capability set includes ``CAP_SYS_TIME``.

    The RTC ``RTC_SET_TIME`` ioctl is gated on this capability in the *initial*
    user namespace, so a rootless container fails the write even when the
    device node is writable. File permissions alone cannot detect that.
    """
    try:
        text = status_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("CapEff:"):
            try:
                mask = int(line.split(":", 1)[1].strip(), 16)
            except ValueError:
                return False
            return bool(mask & (1 << CAP_SYS_TIME))
    return False


def has_binary(name: str) -> bool:
    return shutil.which(name) is not None
