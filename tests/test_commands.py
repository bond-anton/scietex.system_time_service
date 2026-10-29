"""Command layer tests: capability gating and the exact argv issued."""

from pathlib import Path

import pytest

from scietex.system_time_service.clock import commands
from scietex.system_time_service.clock.capabilities import Capabilities
from scietex.system_time_service.clock.platform import PlatformInfo
from scietex.system_time_service.clock.runner import (
    DRY_RUN,
    PERMISSION_DENIED,
    CommandResult,
    RecordingCommandRunner,
)


def _caps(**overrides) -> Capabilities:
    base = {
        "timedatectl": True,
        "timezone_settable": True,
        "clock_settable": True,
        "ntp_controllable": True,
        "ntp_configurable": True,
        "rtc_present": True,
        "battery_available": False,
        "privileged": True,
        "platform_id": "fedora",
        "ntp_daemon": "chrony",
    }
    base.update(overrides)
    return Capabilities(**base)


def _platform(
    confdir: str | None = "/etc/chrony.d",
    timesyncd_confdir: str | None = "/etc/systemd/timesyncd.conf.d",
) -> PlatformInfo:
    return PlatformInfo(
        platform_id="fedora",
        os_name="Fedora Linux",
        os_version="44",
        machine="x86_64",
        is_raspberry_pi=False,
        chrony_confdir=confdir,
        timesyncd_confdir=timesyncd_confdir,
        chrony_confdir_supported=confdir is not None,
    )


def test_set_timezone_rejects_unknown_zone() -> None:
    runner = RecordingCommandRunner()
    result = commands.set_timezone(runner, _caps(), "Mars/Olympus_Mons")

    assert result.ok is False
    assert result.error_code == commands.INVALID_TIMEZONE
    assert runner.recorded == []


def test_set_timezone_issues_timedatectl() -> None:
    runner = RecordingCommandRunner()
    result = commands.set_timezone(runner, _caps(), "Europe/Berlin")

    assert result.ok is True
    assert runner.recorded == [["timedatectl", "set-timezone", "Europe/Berlin"]]


def test_set_timezone_denied_without_privilege() -> None:
    result = commands.set_timezone(
        RecordingCommandRunner(),
        _caps(timezone_settable=False, privileged=False),
        "UTC",
    )
    assert result.error_code == PERMISSION_DENIED


def test_set_ntp_enabled_denied_without_daemon() -> None:
    result = commands.set_ntp_enabled(
        RecordingCommandRunner(), _caps(ntp_controllable=False), True
    )
    assert result.error_code == commands.NTP_UNAVAILABLE


def test_write_ntp_servers_creates_chrony_dropin(tmp_path: Path) -> None:
    runner = RecordingCommandRunner()
    result = commands.write_ntp_servers(
        runner, _caps(), _platform(str(tmp_path)), ["0.pool.ntp.org", "1.pool.ntp.org"]
    )

    assert result.ok is True
    dropin = tmp_path / commands.CHRONY_DROPIN_NAME
    assert dropin.is_file()
    body = dropin.read_text(encoding="utf-8")
    assert commands.MANAGED_MARKER in body
    assert "server 0.pool.ntp.org iburst" in body
    assert "server 1.pool.ntp.org iburst" in body
    assert runner.recorded == [["systemctl", "restart", "chronyd"]]


def test_write_ntp_servers_creates_timesyncd_dropin(tmp_path: Path) -> None:
    runner = RecordingCommandRunner()
    result = commands.write_ntp_servers(
        runner,
        _caps(ntp_daemon="timesyncd"),
        _platform(confdir=None, timesyncd_confdir=str(tmp_path)),
        ["time.cloudflare.com"],
    )
    assert result.ok is True
    body = (tmp_path / commands.TIMESYNCD_DROPIN_NAME).read_text(encoding="utf-8")
    assert "[Time]" in body
    assert "NTP=time.cloudflare.com" in body
    assert runner.recorded == [["systemctl", "restart", "systemd-timesyncd"]]


def test_write_ntp_servers_reports_unsupported_platform() -> None:
    result = commands.write_ntp_servers(
        RecordingCommandRunner(),
        _caps(ntp_configurable=False),
        _platform(None),
        ["0.pool.ntp.org"],
    )
    assert result.error_code == commands.UNSUPPORTED_PLATFORM


def test_set_clock_disables_ntp_then_sets_time() -> None:
    runner = RecordingCommandRunner()
    result = commands.set_clock(runner, _caps(), 1793000000.0, write_rtc=False)

    assert result.ok is True
    assert runner.recorded == [
        ["timedatectl", "set-ntp", "false"],
        ["timedatectl", "set-time", "@1793000000.000000"],
    ]


def test_set_clock_writes_rtc_when_requested() -> None:
    runner = RecordingCommandRunner()
    commands.set_clock(runner, _caps(), 1793000000.0, write_rtc=True)

    assert runner.recorded[-1] == ["hwclock", "--systohc", "--utc"]


def test_set_clock_stops_when_ntp_disable_fails() -> None:
    runner = RecordingCommandRunner(
        {
            ("timedatectl", "set-ntp"): CommandResult(
                ok=False, error_code="FAILED", stderr="boom"
            )
        }
    )
    result = commands.set_clock(runner, _caps(), 1793000000.0, write_rtc=False)

    assert result.ok is False
    assert runner.recorded == [["timedatectl", "set-ntp", "false"]]


def test_write_rtc_reports_missing_rtc() -> None:
    result = commands.write_rtc_from_system(
        RecordingCommandRunner(), _caps(rtc_present=False)
    )
    assert result.error_code == commands.RTC_UNAVAILABLE


@pytest.mark.parametrize("enabled", [True, False])
def test_set_rtc_in_local_tz(enabled: bool) -> None:
    runner = RecordingCommandRunner()
    commands.set_rtc_in_local_tz(runner, _caps(), enabled)
    assert runner.recorded == [
        ["timedatectl", "set-local-rtc", "1" if enabled else "0"]
    ]


def test_dry_run_runner_records_mutations_without_running() -> None:
    from scietex.system_time_service.clock.runner import DryRunCommandRunner

    runner = DryRunCommandRunner(read_only_prefixes=(("timedatectl", "show"),))
    result = runner.run(["timedatectl", "set-timezone", "UTC"])

    assert result.ok is True
    assert result.error_code == DRY_RUN
    assert runner.recorded == [["timedatectl", "set-timezone", "UTC"]]
