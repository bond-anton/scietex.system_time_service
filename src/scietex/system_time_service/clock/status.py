"""Read-only status collection.

Every parser here is written against output captured from a real host, and
every one degrades to ``None`` rather than raising: a monitoring endpoint that
crashes on an unexpected line is worse than one that reports a gap.
"""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import msgspec

from .platform import NtpDaemon


class NtpSource(msgspec.Struct, frozen=True):
    """One NTP source as reported by the daemon."""

    name: str
    state: str
    stratum: int | None = None
    reachability: int | None = None
    offset_seconds: float | None = None
    jitter_seconds: float | None = None


class NtpStatus(msgspec.Struct, frozen=True):
    """NTP synchronisation state."""

    daemon: NtpDaemon
    synchronized: bool = False
    reference_id: str | None = None
    stratum: int | None = None
    system_offset_seconds: float | None = None
    rms_offset_seconds: float | None = None
    frequency_ppm: float | None = None
    skew_ppm: float | None = None
    root_delay_seconds: float | None = None
    root_dispersion_seconds: float | None = None
    leap_status: str | None = None
    sources: list[NtpSource] = msgspec.field(default_factory=list)


class RtcStatus(msgspec.Struct, frozen=True):
    """Real-time clock state."""

    present: bool = False
    device: str | None = None
    name: str | None = None
    time_utc: datetime | None = None
    since_epoch: int | None = None
    drift_seconds: float | None = None
    hctosys: bool | None = None
    in_local_tz: bool | None = None


class ClockStatus(msgspec.Struct, frozen=True):
    """System clock state."""

    local_time: datetime | None = None
    utc_time: datetime | None = None
    timezone: str | None = None
    utc_offset_seconds: int | None = None
    ntp_enabled: bool | None = None
    ntp_synchronized: bool | None = None
    rtc_in_local_tz: bool | None = None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_show(output: str) -> dict[str, str]:
    """Parse ``timedatectl show`` key=value output."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def _parse_usec(value: str | None) -> datetime | None:
    """Parse a ``TimeUSec`` value such as ``Thu 2026-10-29 12:44:19 MSK``.

    The trailing zone abbreviation is not a stable identifier, so the value is
    parsed without it and returned as a naive local timestamp; callers pair it
    with the separately reported timezone.
    """
    if not value:
        return None
    parts = value.split()
    if len(parts) < 3:
        return None
    stamp = f"{parts[1]} {parts[2]}"
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _utc_offset(tz_name: str | None) -> int | None:
    """Return the zone's current UTC offset in seconds.

    ``timedatectl`` reports the zone name but not the offset, and the trailing
    abbreviation in ``TimeUSec`` is not a stable identifier. An unknown zone
    yields ``None`` rather than a guessed offset.
    """
    if not tz_name:
        return None
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    delta = datetime.now(zone).utcoffset()
    return int(delta.total_seconds()) if delta is not None else None


def read_clock_status(runner, *, rtc_sysfs: str) -> ClockStatus:
    """Read the system clock via ``timedatectl show``."""
    result = runner.run(["timedatectl", "show"], timeout=5.0)
    if not result.ok:
        return ClockStatus()
    values = _parse_show(result.stdout)
    tz_name = values.get("Timezone") or None
    offset = _utc_offset(tz_name)
    return ClockStatus(
        local_time=_parse_usec(values.get("TimeUSec")),
        utc_time=_parse_usec(values.get("RTCTimeUSec")),
        timezone=tz_name,
        utc_offset_seconds=offset,
        ntp_enabled=_parse_bool(values.get("NTP")),
        ntp_synchronized=_parse_bool(values.get("NTPSynchronized")),
        rtc_in_local_tz=_parse_bool(values.get("LocalRTC")),
    )


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"yes", "true", "1"}:
        return True
    if lowered in {"no", "false", "0"}:
        return False
    return None


def read_ntp_status(runner, daemon: NtpDaemon) -> NtpStatus:
    """Read NTP state from whichever daemon is active."""
    if daemon == "chrony":
        return _read_chrony(runner)
    if daemon == "timesyncd":
        return _read_timesyncd(runner)
    return NtpStatus(daemon=daemon)


def _read_chrony(runner) -> NtpStatus:
    """Parse ``chronyc -c tracking`` and ``chronyc -c sources``.

    The CSV forms are used because the human-readable tables pad columns to the
    widest value, which shifts between chrony versions.
    """
    tracking = runner.run(["chronyc", "-c", "tracking"], timeout=5.0)
    if not tracking.ok:
        return NtpStatus(daemon="chrony")

    fields = tracking.stdout.strip().split(",")
    # Reference ID, address, stratum, ref time, system offset, last offset,
    # RMS offset, frequency, residual freq, skew, root delay, root dispersion,
    # update interval, leap status
    if len(fields) < 14:
        return NtpStatus(daemon="chrony")

    sources: list[NtpSource] = []
    sources_result = runner.run(["chronyc", "-c", "sources"], timeout=5.0)
    if sources_result.ok:
        for line in sources_result.stdout.splitlines():
            parts = line.strip().split(",")
            # mode, state, address, stratum, poll, reach, last rx, last sample,
            # last offset, correction
            if len(parts) < 10:
                continue
            sources.append(
                NtpSource(
                    name=parts[2],
                    state=parts[1],
                    stratum=_int(parts[3]),
                    reachability=_int(parts[5]),
                    offset_seconds=_float(parts[8]),
                    jitter_seconds=_float(parts[9]),
                )
            )

    return NtpStatus(
        daemon="chrony",
        synchronized=fields[1] != "0.0.0.0" and fields[1] != "",
        reference_id=fields[0] or None,
        stratum=_int(fields[2]),
        system_offset_seconds=_float(fields[4]),
        rms_offset_seconds=_float(fields[6]),
        frequency_ppm=_float(fields[7]),
        skew_ppm=_float(fields[9]),
        root_delay_seconds=_float(fields[10]),
        root_dispersion_seconds=_float(fields[11]),
        leap_status=fields[13] or None,
        sources=sources,
    )


def _read_timesyncd(runner) -> NtpStatus:
    """Parse ``timedatectl show-timesync``."""
    result = runner.run(["timedatectl", "show-timesync"], timeout=5.0)
    if not result.ok:
        return NtpStatus(daemon="timesyncd")
    values = _parse_show(result.stdout)
    server = values.get("ServerName") or values.get("ServerAddress")
    sources = [NtpSource(name=server, state="*")] if server else []
    return NtpStatus(
        daemon="timesyncd",
        synchronized=_parse_bool(values.get("NTPSynchronized")) or False,
        stratum=_int(values.get("Stratum", "")),
        root_delay_seconds=_float(values.get("RootDelay", "")),
        root_dispersion_seconds=_float(values.get("RootDispersion", "")),
        leap_status=values.get("LeapStatus") or None,
        sources=sources,
    )


def read_rtc_status(
    runner, *, rtc_sysfs: str, rtc_device: str, in_local_tz: bool | None
) -> RtcStatus:
    """Read the RTC from sysfs.

    sysfs is used rather than ``hwclock`` because ``/dev/rtc0`` is
    ``root:clock`` and the service may run unprivileged in dry-run mode. The
    trade-off is one-second resolution on ``since_epoch``.
    """
    root = Path(rtc_sysfs)
    since_epoch_path = root / "since_epoch"
    if not since_epoch_path.exists():
        return RtcStatus(present=False, device=rtc_device, in_local_tz=in_local_tz)

    def _read(name: str) -> str | None:
        path = root / name
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    since_epoch = _int(_read("since_epoch") or "")
    system_epoch = int(datetime.now(timezone.utc).timestamp())
    drift = float(since_epoch - system_epoch) if since_epoch is not None else None
    rtc_time = None
    if since_epoch is not None:
        rtc_time = datetime.fromtimestamp(since_epoch, tz=timezone.utc)

    return RtcStatus(
        present=True,
        device=rtc_device,
        name=_read("name"),
        time_utc=rtc_time,
        since_epoch=since_epoch,
        drift_seconds=drift,
        hctosys=_parse_bool(_read("hctosys")),
        in_local_tz=in_local_tz,
    )
