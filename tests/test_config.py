"""Config bounds: a bad RTC sync interval is rejected at resolution time."""

import msgspec
import pytest
from scietex.service.config_merge import resolve_section

from scietex.system_time_service.config import SystemTimeSettings


@pytest.mark.parametrize("interval", [59, 30, 0, -1, 86_401])
def test_rtc_sync_interval_below_or_above_bounds_is_rejected(interval: int) -> None:
    with pytest.raises(msgspec.ValidationError):
        resolve_section(SystemTimeSettings, [{"rtc_sync_interval_seconds": interval}])


@pytest.mark.parametrize("interval", [None, 60, 86_400])
def test_rtc_sync_interval_within_bounds_is_accepted(interval: int | None) -> None:
    settings = resolve_section(
        SystemTimeSettings, [{"rtc_sync_interval_seconds": interval}]
    )

    assert settings.rtc_sync_interval_seconds == interval
