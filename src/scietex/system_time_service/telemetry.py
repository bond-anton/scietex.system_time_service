"""Telemetry payload published to Valkey.

The struct deliberately omits ``forbid_unknown_fields`` so the API can decode
it with a smaller read copy and a newer worker's extra fields do not break an
older API.
"""

from datetime import datetime, timezone

import msgspec

from .clock.battery import BatteryStatus
from .clock.capabilities import Capabilities
from .clock.status import ClockStatus, NtpStatus, RtcStatus

TELEMETRY_INTERVAL: float = 2.0
TELEMETRY_TTL: int = 6
SCHEMA_VERSION: int = 1


class SystemTimeTelemetry(msgspec.Struct):
    """Everything the UI needs to render the date and time page."""

    service: str
    instance_id: str
    clock: ClockStatus
    ntp: NtpStatus
    rtc: RtcStatus
    battery: BatteryStatus
    capabilities: Capabilities
    clock_source: str
    pending_apply: bool = False
    last_error: str | None = None
    last_rtc_sync_at: datetime | None = None
    last_rtc_sync_error: str | None = None
    timestamp: datetime = msgspec.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    schema_version: int = SCHEMA_VERSION


def build_telemetry(
    *,
    service: str,
    instance_id: str,
    clock: ClockStatus,
    ntp: NtpStatus,
    rtc: RtcStatus,
    battery: BatteryStatus,
    capabilities: Capabilities,
    clock_source: str,
    pending_apply: bool,
    last_error: str | None,
    last_rtc_sync_at: datetime | None,
    last_rtc_sync_error: str | None,
) -> SystemTimeTelemetry:
    return SystemTimeTelemetry(
        service=service,
        instance_id=instance_id,
        clock=clock,
        ntp=ntp,
        rtc=rtc,
        battery=battery,
        capabilities=capabilities,
        clock_source=clock_source,
        pending_apply=pending_apply,
        last_error=last_error,
        last_rtc_sync_at=last_rtc_sync_at,
        last_rtc_sync_error=last_rtc_sync_error,
    )
