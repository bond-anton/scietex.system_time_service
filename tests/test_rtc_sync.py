"""Periodic RTC write: when it runs, what it writes, and what it records."""

import time
from pathlib import Path

from scietex.system_time_service.clock.runner import CommandResult
from scietex.system_time_service.config import SystemTimeSettings
from scietex.system_time_service.system_time_worker import sync_rtc
from tests.fixtures import TIMEDATECTL_SHOW
from tests.helpers import _caps, _runner, _runtime

LOCAL_TZ_SHOW = TIMEDATECTL_SHOW.replace("LocalRTC=no", "LocalRTC=yes")


def _settings(tmp_path: Path, **overrides) -> SystemTimeSettings:
    values = {
        "rtc_sysfs": str(tmp_path / "rtc"),
        "rtc_sync_interval_seconds": 600,
    }
    values.update(overrides)
    return SystemTimeSettings(**values)


def _rtc_sysfs(tmp_path: Path, offset_seconds: int = 0) -> None:
    root = tmp_path / "rtc"
    root.mkdir(parents=True, exist_ok=True)
    (root / "since_epoch").write_text(
        f"{int(time.time()) + offset_seconds}\n", encoding="utf-8"
    )


def test_sync_skipped_when_interval_is_disabled(tmp_path: Path) -> None:
    runner = _runner()
    runtime = _runtime(
        tmp_path, _settings(tmp_path, rtc_sync_interval_seconds=None), runner
    )

    outcome = sync_rtc(runtime)

    assert outcome.attempted is False
    assert runner.recorded == []


def test_sync_skipped_without_write_capability(tmp_path: Path) -> None:
    """An unprivileged host must not carry a permanent error for this feature."""
    _rtc_sysfs(tmp_path, offset_seconds=30)
    runner = _runner()
    runtime = _runtime(tmp_path, _settings(tmp_path), runner)
    runtime.capabilities = _caps(rtc_writable=False)

    outcome = sync_rtc(runtime)

    assert outcome.attempted is False
    assert runtime.last_rtc_sync_error is None
    assert ["hwclock", "--systohc", "--utc"] not in runner.recorded


def test_sync_skipped_while_ntp_is_unsynchronized(tmp_path: Path) -> None:
    """Auto mode with NTP still converging: the system clock may be the wrong one."""
    _rtc_sysfs(tmp_path, offset_seconds=30)
    runner = _runner({("chronyc", "-c", "tracking"): CommandResult(ok=True, stdout="")})
    runtime = _runtime(tmp_path, _settings(tmp_path, clock_source="auto"), runner)

    outcome = sync_rtc(runtime)

    assert outcome.attempted is False
    assert ["hwclock", "--systohc", "--utc"] not in runner.recorded


def test_sync_records_success_without_writing_when_in_tolerance(
    tmp_path: Path,
) -> None:
    _rtc_sysfs(tmp_path)
    runner = _runner()
    runtime = _runtime(tmp_path, _settings(tmp_path), runner)

    outcome = sync_rtc(runtime)

    assert outcome.attempted is False
    assert outcome.written is False
    assert runtime.last_rtc_sync_at is not None
    assert runtime.last_rtc_sync_error is None
    assert not any(argv[0] == "hwclock" for argv in runner.recorded)


def test_sync_writes_rtc_when_drift_exceeds_tolerance(tmp_path: Path) -> None:
    _rtc_sysfs(tmp_path, offset_seconds=30)
    runner = _runner()
    runtime = _runtime(tmp_path, _settings(tmp_path), runner)

    outcome = sync_rtc(runtime)

    assert outcome.attempted is True
    assert outcome.written is True
    assert runner.recorded.count(["hwclock", "--systohc", "--utc"]) == 1
    assert runtime.last_rtc_sync_at is not None
    assert runtime.last_rtc_sync_error is None


def test_sync_records_write_failure_without_touching_last_sync(tmp_path: Path) -> None:
    _rtc_sysfs(tmp_path, offset_seconds=30)
    runner = _runner(
        {
            ("hwclock", "--systohc"): CommandResult(
                ok=False, error_code="FAILED", stderr="boom"
            )
        }
    )
    runtime = _runtime(tmp_path, _settings(tmp_path), runner)

    outcome = sync_rtc(runtime)

    assert outcome.attempted is True
    assert outcome.written is False
    assert runtime.last_rtc_sync_error == "boom"
    assert runtime.last_rtc_sync_at is None


def test_sync_offset_is_corrected_for_a_local_time_rtc(tmp_path: Path) -> None:
    """A local-time RTC reports the UTC offset as drift, not a real error."""
    _rtc_sysfs(tmp_path, offset_seconds=10800)
    runner = _runner(
        {("timedatectl", "show"): CommandResult(ok=True, stdout=LOCAL_TZ_SHOW)}
    )
    runtime = _runtime(tmp_path, _settings(tmp_path, rtc_in_local_tz=True), runner)

    outcome = sync_rtc(runtime)

    assert outcome.attempted is False
    assert not any(argv[0] == "hwclock" for argv in runner.recorded)
    assert runtime.last_rtc_sync_at is not None
