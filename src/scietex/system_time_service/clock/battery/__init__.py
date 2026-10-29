"""RTC battery providers.

Battery state is not uniformly exposed across hardware. Each provider reports
whether it can answer for this host; the resolver picks the first one that can,
and the null provider answers "not available" when none can. Nothing here
guesses a value: an unreadable battery is reported as unavailable, never as 0%.
"""

import json
from pathlib import Path
from typing import Protocol

import msgspec

from ..platform import PlatformInfo
from ..runner import CommandRunner


class BatteryStatus(msgspec.Struct, frozen=True):
    """RTC battery state. Every measurement is optional."""

    available: bool = False
    provider: str | None = None
    charge_percent: float | None = None
    voltage_volts: float | None = None
    status: str | None = None
    detail: str | None = None


class RtcBatteryProvider(Protocol):
    """Reads RTC battery state for one hardware family."""

    name: str

    def available(self) -> bool: ...

    def read(self, runner: CommandRunner) -> BatteryStatus: ...


class NullBatteryProvider:
    """Reports that this hardware exposes no battery telemetry."""

    name = "none"

    def __init__(self, detail: str = "not exposed by this hardware") -> None:
        self._detail = detail

    def available(self) -> bool:
        return False

    def read(self, runner: CommandRunner) -> BatteryStatus:
        return BatteryStatus(available=False, provider=None, detail=self._detail)


class SysfsPowerSupplyProvider:
    """Reads a battery from ``/sys/class/power_supply``.

    Covers boards that expose the RTC backup cell as a power supply. On hosts
    where the directory is empty this provider reports unavailable.
    """

    name = "sysfs"

    def __init__(self, root: str = "/sys/class/power_supply") -> None:
        self._root = Path(root)

    def _battery_dir(self) -> Path | None:
        if not self._root.is_dir():
            return None
        for entry in sorted(self._root.iterdir()):
            type_path = entry / "type"
            try:
                if type_path.read_text(encoding="utf-8").strip() == "battery":
                    return entry
            except OSError:
                continue
        return None

    def available(self) -> bool:
        return self._battery_dir() is not None

    def read(self, runner: CommandRunner) -> BatteryStatus:
        directory = self._battery_dir()
        if directory is None:
            return BatteryStatus(
                available=False,
                provider=None,
                detail="no battery in /sys/class/power_supply",
            )

        def _read(name: str) -> str | None:
            try:
                return (directory / name).read_text(encoding="utf-8").strip()
            except OSError:
                return None

        charge = _read("capacity")
        voltage_uv = _read("voltage_now")
        return BatteryStatus(
            available=True,
            provider=self.name,
            charge_percent=float(charge)
            if charge and charge.lstrip("-").isdigit()
            else None,
            voltage_volts=float(voltage_uv) / 1_000_000
            if voltage_uv and voltage_uv.lstrip("-").isdigit()
            else None,
            status=_read("status"),
        )


class CommandBatteryProvider:
    """Reads battery state from an operator-configured command.

    The command must print a JSON object with any of ``charge_percent``,
    ``voltage_volts``, ``status``. This is the escape hatch for boards whose
    battery is only reachable through a vendor tool, so no per-board parsing
    lives in this package.
    """

    name = "command"

    def __init__(self, command: str | None, *, timeout: float = 3.0) -> None:
        self._command = command
        self._timeout = timeout

    def available(self) -> bool:
        return bool(self._command)

    def read(self, runner: CommandRunner) -> BatteryStatus:
        if not self._command:
            return BatteryStatus(
                available=False, provider=None, detail="no battery command configured"
            )
        result = runner.run(["sh", "-c", self._command], timeout=self._timeout)
        if not result.ok:
            return BatteryStatus(
                available=False,
                provider=self.name,
                detail=f"battery command failed: {result.error_code or result.stderr.strip()}",
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return BatteryStatus(
                available=False,
                provider=self.name,
                detail="battery command did not return JSON",
            )
        if not isinstance(payload, dict):
            return BatteryStatus(
                available=False,
                provider=self.name,
                detail="battery command did not return an object",
            )
        return BatteryStatus(
            available=True,
            provider=self.name,
            charge_percent=_as_float(payload.get("charge_percent")),
            voltage_volts=_as_float(payload.get("voltage_volts")),
            status=payload.get("status")
            if isinstance(payload.get("status"), str)
            else None,
        )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def resolve_provider(
    *,
    provider: str,
    platform_info: PlatformInfo,
    battery_command: str | None,
    battery_command_timeout: float = 3.0,
    sysfs_root: str = "/sys/class/power_supply",
) -> RtcBatteryProvider:
    """Pick the battery provider for this host.

    ``auto`` tries sysfs first, then the configured command. An explicit
    provider name is honoured as-is, so an operator can pin one. Raspberry Pi
    hosts get the command provider ahead of sysfs because their RTC cell is not
    a power supply.
    """
    sysfs = SysfsPowerSupplyProvider(sysfs_root)
    command = CommandBatteryProvider(battery_command, timeout=battery_command_timeout)

    if provider == "none":
        return NullBatteryProvider("battery reporting disabled by configuration")
    if provider == "sysfs":
        return sysfs
    if provider == "command":
        return command

    if platform_info.is_raspberry_pi and command.available():
        return command
    if sysfs.available():
        return sysfs
    if command.available():
        return command
    return NullBatteryProvider()
