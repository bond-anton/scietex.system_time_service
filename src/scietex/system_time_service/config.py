"""Service settings and the YAML bootstrap loader.

The settings struct is the L0 base of the four-layer config merge. Only the
fields an operator can meaningfully change are exposed remotely; the rest are
host-local and come from ``system_time.yml``.
"""

from pathlib import Path
from typing import Literal

import msgspec

SYSTEM_TIME_SECTION: str = "system_time"
SYSTEM_TIME_CONFIG_SUBDIR: str = "system_time"
SYSTEM_TIME_CONFIG_FILE: str = "system_time.yml"

ClockSource = Literal["auto", "manual"]
BatteryProviderName = Literal["auto", "none", "sysfs", "command"]


class SystemTimeSettings(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Desired system time state plus host-local tuning.

    ``timezone`` is ``None`` when the operator has not chosen one, which means
    "leave the host alone" rather than "reset to UTC".
    """

    timezone: str | None = None
    clock_source: ClockSource = "auto"
    ntp_servers: list[str] = msgspec.field(default_factory=list)
    rtc_in_local_tz: bool = False

    # Host-local tuning. Never delivered remotely: the API's config patch only
    # carries the four desired-state fields above.
    status_interval: float = 5.0
    rtc_device: str = "/dev/rtc0"
    rtc_sysfs: str = "/sys/class/rtc/rtc0"
    battery_provider: BatteryProviderName = "auto"
    battery_command: str | None = None
    battery_command_timeout: float = 3.0


SYSTEM_TIME_SETTINGS_DEFAULTS = SystemTimeSettings()

#: Fields the API may patch remotely. Anything else is host-local.
REMOTE_FIELDS: frozenset[str] = frozenset(
    {"timezone", "clock_source", "ntp_servers", "rtc_in_local_tz"}
)


def read_system_time_config(
    conf_dir: str | Path, *, create_default: bool = True
) -> dict:
    """Read ``<conf_dir>/system_time/system_time.yml`` as an L1 bootstrap patch.

    Returns an empty mapping when the file is absent and ``create_default`` is
    false. When ``create_default`` is true the file is written from the
    defaults first, so an operator has something to edit.
    """
    path = Path(conf_dir) / SYSTEM_TIME_CONFIG_SUBDIR / SYSTEM_TIME_CONFIG_FILE
    if not path.is_file():
        if not create_default:
            return {}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            msgspec.yaml.encode(SYSTEM_TIME_SETTINGS_DEFAULTS).decode(),
            encoding="utf-8",
        )
        return {}
    raw = msgspec.yaml.decode(path.read_bytes())
    if not isinstance(raw, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return raw
