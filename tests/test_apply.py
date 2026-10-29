"""Settings application tests: only differences are applied, failures stop the run."""

from pathlib import Path

from scietex.system_time_service.clock.capabilities import Capabilities
from scietex.system_time_service.clock.platform import PlatformInfo
from scietex.system_time_service.clock.runner import (
    CommandResult,
    RecordingCommandRunner,
)
from scietex.system_time_service.config import SystemTimeSettings
from scietex.system_time_service.runtime import ClockRuntime
from scietex.system_time_service.system_time_worker import apply_settings
from tests.fixtures import CHRONYC_SOURCES_CSV, CHRONYC_TRACKING_CSV, TIMEDATECTL_SHOW


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


def _runtime(
    tmp_path: Path, settings: SystemTimeSettings, runner: RecordingCommandRunner
) -> ClockRuntime:
    from scietex.system_time_service.clock.battery import NullBatteryProvider

    platform_info = PlatformInfo(
        platform_id="fedora",
        os_name="Fedora Linux",
        os_version="44",
        machine="x86_64",
        is_raspberry_pi=False,
        chrony_confdir=str(tmp_path),
        timesyncd_confdir=str(tmp_path),
        chrony_confdir_supported=True,
    )
    return ClockRuntime(
        runner=runner,
        platform_info=platform_info,
        battery_provider=NullBatteryProvider(),
        settings=settings,
        capabilities=_caps(),
    )


def _runner(
    extra: dict[tuple[str, ...], CommandResult] | None = None,
) -> RecordingCommandRunner:
    responses = {
        ("timedatectl", "show"): CommandResult(ok=True, stdout=TIMEDATECTL_SHOW),
        ("chronyc", "-c", "tracking"): CommandResult(
            ok=True, stdout=CHRONYC_TRACKING_CSV
        ),
        ("chronyc", "-c", "sources"): CommandResult(
            ok=True, stdout=CHRONYC_SOURCES_CSV
        ),
    }
    responses.update(extra or {})
    return RecordingCommandRunner(responses)


def test_apply_is_a_noop_when_settings_match_host(tmp_path: Path) -> None:
    """The host already reports Europe/Moscow, NTP on, RTC in UTC."""
    runner = _runner()
    runtime = _runtime(
        tmp_path,
        SystemTimeSettings(
            timezone="Europe/Moscow", clock_source="auto", rtc_in_local_tz=False
        ),
        runner,
    )

    result = apply_settings(runtime)

    assert result.ok is True
    mutations = [
        argv
        for argv in runner.recorded
        if argv[0] != "timedatectl" or argv[1] != "show"
    ]
    mutations = [argv for argv in mutations if argv[:2] != ["chronyc", "-c"]]
    # Only the NTP drop-in write and its restart remain, because the server list
    # is always rewritten to keep the drop-in authoritative.
    assert mutations == [["systemctl", "restart", "chronyd"]]


def test_apply_sets_timezone_when_it_differs(tmp_path: Path) -> None:
    runner = _runner()
    runtime = _runtime(tmp_path, SystemTimeSettings(timezone="Europe/Berlin"), runner)

    apply_settings(runtime)

    assert ["timedatectl", "set-timezone", "Europe/Berlin"] in runner.recorded


def test_apply_disables_ntp_for_manual_source(tmp_path: Path) -> None:
    runner = _runner()
    runtime = _runtime(
        tmp_path,
        SystemTimeSettings(timezone="Europe/Moscow", clock_source="manual"),
        runner,
    )

    apply_settings(runtime)

    assert ["timedatectl", "set-ntp", "false"] in runner.recorded


def test_apply_stops_at_first_failure(tmp_path: Path) -> None:
    runner = _runner(
        {
            ("timedatectl", "set-timezone"): CommandResult(
                ok=False, error_code="FAILED", stderr="denied"
            )
        }
    )
    runtime = _runtime(
        tmp_path,
        SystemTimeSettings(timezone="Europe/Berlin", clock_source="manual"),
        runner,
    )

    result = apply_settings(runtime)

    assert result.ok is False
    assert result.stderr == "denied"
    assert ["timedatectl", "set-ntp", "false"] not in runner.recorded


def test_apply_skips_ntp_servers_when_not_configurable(tmp_path: Path) -> None:
    runner = _runner()
    runtime = _runtime(tmp_path, SystemTimeSettings(timezone="Europe/Moscow"), runner)
    runtime.capabilities = _caps(ntp_configurable=False)

    apply_settings(runtime)

    assert ["systemctl", "restart", "chronyd"] not in runner.recorded
