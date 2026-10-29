"""Battery provider tests.

The contract that matters: an unreadable battery is reported unavailable, never
as a zero reading.
"""

import json
from pathlib import Path

from scietex.system_time_service.clock.battery import (
    CommandBatteryProvider,
    NullBatteryProvider,
    SysfsPowerSupplyProvider,
    resolve_provider,
)
from scietex.system_time_service.clock.platform import PlatformInfo
from scietex.system_time_service.clock.runner import (
    CommandResult,
    RecordingCommandRunner,
)


def _platform(is_pi: bool = False) -> PlatformInfo:
    return PlatformInfo(
        platform_id="raspberry-pi-os" if is_pi else "fedora",
        os_name="test",
        os_version="1",
        machine="aarch64" if is_pi else "x86_64",
        is_raspberry_pi=is_pi,
        chrony_confdir="/etc/chrony.d",
        timesyncd_confdir="/etc/systemd/timesyncd.conf.d",
        chrony_confdir_supported=True,
    )


def test_null_provider_reports_unavailable() -> None:
    status = NullBatteryProvider().read(RecordingCommandRunner())
    assert status.available is False
    assert status.charge_percent is None


def test_sysfs_provider_without_battery(tmp_path: Path) -> None:
    provider = SysfsPowerSupplyProvider(str(tmp_path))
    assert provider.available() is False
    assert provider.read(RecordingCommandRunner()).available is False


def test_sysfs_provider_reads_battery(tmp_path: Path) -> None:
    battery = tmp_path / "BAT0"
    battery.mkdir()
    (battery / "type").write_text("battery\n", encoding="utf-8")
    (battery / "capacity").write_text("87\n", encoding="utf-8")
    (battery / "voltage_now").write_text("3000000\n", encoding="utf-8")
    (battery / "status").write_text("Discharging\n", encoding="utf-8")

    provider = SysfsPowerSupplyProvider(str(tmp_path))
    status = provider.read(RecordingCommandRunner())

    assert status.available is True
    assert status.charge_percent == 87.0
    assert status.voltage_volts == 3.0
    assert status.status == "Discharging"


def test_sysfs_provider_ignores_non_battery_entries(tmp_path: Path) -> None:
    ac = tmp_path / "AC"
    ac.mkdir()
    (ac / "type").write_text("mains\n", encoding="utf-8")

    assert SysfsPowerSupplyProvider(str(tmp_path)).available() is False


def test_command_provider_parses_json() -> None:
    runner = RecordingCommandRunner(
        {
            ("sh", "-c"): CommandResult(
                ok=True, stdout=json.dumps({"charge_percent": 64, "voltage_volts": 3.1})
            )
        }
    )
    status = CommandBatteryProvider("read-battery").read(runner)

    assert status.available is True
    assert status.charge_percent == 64.0
    assert status.voltage_volts == 3.1


def test_command_provider_rejects_non_json() -> None:
    runner = RecordingCommandRunner(
        {("sh", "-c"): CommandResult(ok=True, stdout="not json")}
    )
    status = CommandBatteryProvider("read-battery").read(runner)

    assert status.available is False
    assert "JSON" in (status.detail or "")


def test_command_provider_reports_failure() -> None:
    runner = RecordingCommandRunner(
        {("sh", "-c"): CommandResult(ok=False, error_code="MISSING_BINARY")}
    )
    status = CommandBatteryProvider("read-battery").read(runner)

    assert status.available is False
    assert "failed" in (status.detail or "")


def test_resolve_prefers_command_on_raspberry_pi(tmp_path: Path) -> None:
    battery = tmp_path / "BAT0"
    battery.mkdir()
    (battery / "type").write_text("battery\n", encoding="utf-8")

    provider = resolve_provider(
        provider="auto",
        platform_info=_platform(is_pi=True),
        battery_command="read-battery",
        sysfs_root=str(tmp_path),
    )

    assert provider.name == "command"


def test_resolve_prefers_sysfs_on_generic_host(tmp_path: Path) -> None:
    battery = tmp_path / "BAT0"
    battery.mkdir()
    (battery / "type").write_text("battery\n", encoding="utf-8")

    provider = resolve_provider(
        provider="auto",
        platform_info=_platform(),
        battery_command="read-battery",
        sysfs_root=str(tmp_path),
    )

    assert provider.name == "sysfs"


def test_resolve_falls_back_to_null(tmp_path: Path) -> None:
    provider = resolve_provider(
        provider="auto",
        platform_info=_platform(),
        battery_command=None,
        sysfs_root=str(tmp_path),
    )

    assert provider.name == "none"
    assert provider.available() is False


def test_resolve_honours_explicit_none() -> None:
    provider = resolve_provider(
        provider="none",
        platform_info=_platform(),
        battery_command="read-battery",
    )
    assert provider.name == "none"
