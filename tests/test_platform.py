"""Platform detection tests.

Each case pins the NTP drop-in layout the service will use, because writing to
the wrong directory silently does nothing on the host.
"""

from pathlib import Path

import pytest

from scietex.system_time_service.clock.platform import detect_platform
from tests.fixtures import (
    OS_RELEASE_ARCH,
    OS_RELEASE_DEBIAN,
    OS_RELEASE_FEDORA,
    OS_RELEASE_UBUNTU_22,
    OS_RELEASE_UBUNTU_24,
    RPI_MODEL,
)


def _detect(tmp_path: Path, os_release: str, model: str = "") -> object:
    os_path = tmp_path / "os-release"
    os_path.write_text(os_release, encoding="utf-8")
    model_path = tmp_path / "model"
    if model:
        model_path.write_text(model, encoding="utf-8")
    return detect_platform(
        os_release_path=os_path, device_model_path=model_path, machine="x86_64"
    )


def test_fedora_uses_chrony_d(tmp_path: Path) -> None:
    info = _detect(tmp_path, OS_RELEASE_FEDORA)
    assert info.platform_id == "fedora"
    assert info.chrony_confdir == "/etc/chrony.d"
    assert info.chrony_confdir_supported is True


def test_ubuntu_24_uses_chrony_conf_d(tmp_path: Path) -> None:
    info = _detect(tmp_path, OS_RELEASE_UBUNTU_24)
    assert info.platform_id == "ubuntu"
    assert info.chrony_confdir == "/etc/chrony/conf.d"
    assert info.chrony_confdir_supported is True


def test_ubuntu_22_reports_chrony_confdir_unsupported(tmp_path: Path) -> None:
    """Ubuntu before 24 has no chrony confdir support, so no drop-in is offered."""
    info = _detect(tmp_path, OS_RELEASE_UBUNTU_22)
    assert info.platform_id == "ubuntu"
    assert info.chrony_confdir is None
    assert info.chrony_confdir_supported is False


def test_debian_uses_chrony_conf_d(tmp_path: Path) -> None:
    info = _detect(tmp_path, OS_RELEASE_DEBIAN)
    assert info.platform_id == "debian"
    assert info.chrony_confdir == "/etc/chrony/conf.d"


def test_raspberry_pi_is_detected_from_device_tree(tmp_path: Path) -> None:
    info = _detect(tmp_path, OS_RELEASE_DEBIAN, model=RPI_MODEL)
    assert info.platform_id == "raspberry-pi-os"
    assert info.is_raspberry_pi is True


def test_unknown_platform_offers_no_dropin(tmp_path: Path) -> None:
    info = _detect(tmp_path, OS_RELEASE_ARCH)
    assert info.platform_id == "unknown"
    assert info.chrony_confdir is None
    assert info.timesyncd_confdir is None


def test_missing_os_release_is_unknown(tmp_path: Path) -> None:
    info = detect_platform(
        os_release_path=tmp_path / "absent",
        device_model_path=tmp_path / "absent-model",
        machine="x86_64",
    )
    assert info.platform_id == "unknown"


@pytest.mark.parametrize(
    "release", [OS_RELEASE_FEDORA, OS_RELEASE_DEBIAN, OS_RELEASE_UBUNTU_24]
)
def test_supported_platforms_expose_timesyncd_dropin(
    tmp_path: Path, release: str
) -> None:
    info = _detect(tmp_path, release)
    assert info.timesyncd_confdir == "/etc/systemd/timesyncd.conf.d"
