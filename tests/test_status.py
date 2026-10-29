"""Status parsing tests against output captured from a real host."""

from scietex.system_time_service.clock.runner import (
    CommandResult,
    RecordingCommandRunner,
)
from scietex.system_time_service.clock.status import (
    read_clock_status,
    read_ntp_status,
    read_rtc_status,
)
from tests.fixtures import CHRONYC_SOURCES_CSV, CHRONYC_TRACKING_CSV, TIMEDATECTL_SHOW


def test_clock_status_parses_timedatectl_show() -> None:
    runner = RecordingCommandRunner(
        {("timedatectl", "show"): CommandResult(ok=True, stdout=TIMEDATECTL_SHOW)}
    )
    status = read_clock_status(runner, rtc_sysfs="/nonexistent")

    assert status.timezone == "Europe/Moscow"
    assert status.utc_offset_seconds == 10800
    assert status.ntp_enabled is True
    assert status.ntp_synchronized is True
    assert status.rtc_in_local_tz is False
    assert status.local_time is not None
    assert status.local_time.hour == 12


def test_clock_status_degrades_when_timedatectl_fails() -> None:
    runner = RecordingCommandRunner(
        {("timedatectl", "show"): CommandResult(ok=False, error_code="MISSING_BINARY")}
    )
    status = read_clock_status(runner, rtc_sysfs="/nonexistent")

    assert status.timezone is None
    assert status.ntp_synchronized is None


def test_chrony_status_parses_csv_tracking_and_sources() -> None:
    runner = RecordingCommandRunner(
        {
            ("chronyc", "-c", "tracking"): CommandResult(
                ok=True, stdout=CHRONYC_TRACKING_CSV
            ),
            ("chronyc", "-c", "sources"): CommandResult(
                ok=True, stdout=CHRONYC_SOURCES_CSV
            ),
        }
    )
    status = read_ntp_status(runner, "chrony")

    assert status.daemon == "chrony"
    assert status.synchronized is True
    assert status.stratum == 4
    assert status.system_offset_seconds == 0.000120549
    assert status.frequency_ppm == -6.283
    assert status.leap_status == "Normal"
    assert len(status.sources) == 3
    assert status.sources[1].state == "*"
    assert status.sources[1].name == "162.159.200.123"
    assert status.sources[1].reachability == 377


def test_chrony_status_handles_truncated_tracking() -> None:
    """A short line must not raise IndexError."""
    runner = RecordingCommandRunner(
        {("chronyc", "-c", "tracking"): CommandResult(ok=True, stdout="A,B\n")}
    )
    status = read_ntp_status(runner, "chrony")

    assert status.daemon == "chrony"
    assert status.synchronized is False


def test_ntp_status_without_daemon() -> None:
    status = read_ntp_status(RecordingCommandRunner(), "none")
    assert status.daemon == "none"
    assert status.synchronized is False


def test_rtc_status_reads_sysfs(tmp_path) -> None:
    (tmp_path / "since_epoch").write_text("1793267060\n", encoding="utf-8")
    (tmp_path / "name").write_text("rtc_cmos PNP0B00:00\n", encoding="utf-8")
    (tmp_path / "hctosys").write_text("1\n", encoding="utf-8")

    status = read_rtc_status(
        RecordingCommandRunner(),
        rtc_sysfs=str(tmp_path),
        rtc_device="/dev/rtc0",
        in_local_tz=False,
    )

    assert status.present is True
    assert status.name == "rtc_cmos PNP0B00:00"
    assert status.since_epoch == 1793267060
    assert status.hctosys is True
    assert status.drift_seconds is not None


def test_rtc_status_absent(tmp_path) -> None:
    status = read_rtc_status(
        RecordingCommandRunner(),
        rtc_sysfs=str(tmp_path / "missing"),
        rtc_device="/dev/rtc0",
        in_local_tz=None,
    )

    assert status.present is False
    assert status.drift_seconds is None
