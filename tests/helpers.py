"""Doubles shared by tests that drive a ``ClockRuntime`` over canned output."""

from pathlib import Path

from scietex.system_time_service.clock.battery import NullBatteryProvider
from scietex.system_time_service.clock.capabilities import Capabilities
from scietex.system_time_service.clock.platform import PlatformInfo
from scietex.system_time_service.clock.runner import (
    CommandResult,
    RecordingCommandRunner,
)
from scietex.system_time_service.config import SystemTimeSettings
from scietex.system_time_service.runtime import ClockRuntime
from tests.fixtures import CHRONYC_SOURCES_CSV, CHRONYC_TRACKING_CSV, TIMEDATECTL_SHOW


def _caps(**overrides) -> Capabilities:
    base = {
        "timedatectl": True,
        "timezone_settable": True,
        "clock_settable": True,
        "ntp_controllable": True,
        "ntp_configurable": True,
        "rtc_present": True,
        "rtc_writable": True,
        "battery_available": False,
        "privileged": True,
        "platform_id": "fedora",
        "ntp_daemon": "chrony",
    }
    base.update(overrides)
    return Capabilities(**base)


def _runtime(
    tmp_path: Path,
    settings: SystemTimeSettings,
    runner: RecordingCommandRunner,
) -> ClockRuntime:
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
