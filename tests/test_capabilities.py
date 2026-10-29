"""Capability detection: what the host and this process can actually do.

The RTC cases pin the two failure modes that file permissions alone cannot
express: a rootless container running as uid 0 with the device node owned by an
unmapped host group, and a container that can open the node but lacks
``CAP_SYS_TIME`` in the initial user namespace.
"""

import os
from pathlib import Path

import pytest

from scietex.system_time_service.clock.capabilities import detect_capabilities
from scietex.system_time_service.clock.platform import (
    PlatformInfo,
    has_cap_sys_time,
)
from tests.helpers import _runner


def _platform_info(tmp_path: Path) -> PlatformInfo:
    return PlatformInfo(
        platform_id="fedora",
        os_name="Fedora Linux",
        os_version="44",
        machine="x86_64",
        is_raspberry_pi=False,
        chrony_confdir=str(tmp_path),
        timesyncd_confdir=str(tmp_path),
        chrony_confdir_supported=True,
    )


def _rtc_sysfs(tmp_path: Path) -> Path:
    root = tmp_path / "rtc"
    root.mkdir(parents=True, exist_ok=True)
    (root / "since_epoch").write_text("1793278642\n", encoding="utf-8")
    return root


def _detect(
    tmp_path: Path,
    rtc_device: Path,
    *,
    privileged: bool = True,
    cap_sys_time: bool = True,
):
    return detect_capabilities(
        _runner(),
        _platform_info(tmp_path),
        rtc_sysfs=str(_rtc_sysfs(tmp_path)),
        rtc_device=str(rtc_device),
        battery_available=False,
        privileged=privileged,
        cap_sys_time=cap_sys_time,
    )


def _writable_device(tmp_path: Path) -> Path:
    device = tmp_path / "rtc0"
    device.write_text("", encoding="utf-8")
    device.chmod(0o600)
    return device


def test_writable_device_with_capability_reports_rtc_writable(tmp_path: Path) -> None:
    caps = _detect(tmp_path, _writable_device(tmp_path))

    assert caps.rtc_present is True
    assert caps.rtc_writable is True


def test_writable_device_without_capability_reports_rtc_not_writable(
    tmp_path: Path,
) -> None:
    """A rootless container can open the node but cannot issue RTC_SET_TIME."""
    caps = _detect(tmp_path, _writable_device(tmp_path), cap_sys_time=False)

    assert caps.rtc_present is True
    assert caps.rtc_writable is False


def test_unreadable_device_reports_rtc_not_writable(tmp_path: Path) -> None:
    device = tmp_path / "rtc0"
    device.write_text("", encoding="utf-8")
    device.chmod(0o000)

    caps = _detect(tmp_path, device)

    assert caps.rtc_present is True
    assert caps.rtc_writable is False


def test_absent_device_reports_rtc_not_writable(tmp_path: Path) -> None:
    caps = _detect(tmp_path, tmp_path / "missing-rtc0")

    assert caps.rtc_present is True
    assert caps.rtc_writable is False


def test_absent_sysfs_reports_rtc_absent(tmp_path: Path) -> None:
    caps = detect_capabilities(
        _runner(),
        _platform_info(tmp_path),
        rtc_sysfs=str(tmp_path / "no-rtc"),
        rtc_device=str(_writable_device(tmp_path)),
        battery_available=False,
        privileged=True,
        cap_sys_time=True,
    )

    assert caps.rtc_present is False
    assert caps.rtc_writable is False


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses the permission bits the case relies on"
)
def test_root_euid_does_not_imply_rtc_writable(tmp_path: Path) -> None:
    """The rootless-container case: uid 0, device owned by an unmapped group."""
    device = tmp_path / "rtc0"
    device.write_text("", encoding="utf-8")
    device.chmod(0o000)

    caps = _detect(tmp_path, device, privileged=True)

    assert caps.privileged is True
    assert caps.rtc_writable is False


def test_has_cap_sys_time_reads_the_effective_set(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text(
        "Name:\tpython\nCapInh:\t0000000000000000\n"
        "CapPrm:\t00000000820405fb\nCapEff:\t00000000820405fb\n",
        encoding="utf-8",
    )

    assert has_cap_sys_time(status) is True


def test_has_cap_sys_time_absent_from_the_effective_set(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text(
        "Name:\tpython\nCapPrm:\t00000000800405fb\nCapEff:\t00000000800405fb\n",
        encoding="utf-8",
    )

    assert has_cap_sys_time(status) is False


def test_has_cap_sys_time_without_a_status_file(tmp_path: Path) -> None:
    assert has_cap_sys_time(tmp_path / "missing") is False
