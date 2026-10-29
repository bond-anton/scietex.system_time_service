"""Command execution seam.

Every privileged or host-touching operation goes through a ``CommandRunner``.
Tests substitute a recording runner, and ``--dry-run`` substitutes one that
executes read-only commands for real but records mutating ones without running
them. Nothing else in the package calls ``subprocess`` directly.
"""

import subprocess
from typing import Protocol

import msgspec

#: Error codes shared by the runner and the command layer.
MISSING_BINARY = "MISSING_BINARY"
PERMISSION_DENIED = "PERMISSION_DENIED"
TIMEOUT = "TIMEOUT"
DRY_RUN = "DRY_RUN"
FAILED = "FAILED"


class CommandResult(msgspec.Struct, frozen=True):
    """Outcome of one command invocation."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    error_code: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


class CommandRunner(Protocol):
    """Executes an argv and reports the outcome without raising."""

    def run(self, argv: list[str], *, timeout: float = 5.0) -> CommandResult: ...


class RealCommandRunner:
    """Runs commands for real via ``subprocess``."""

    def run(self, argv: list[str], *, timeout: float = 5.0) -> CommandResult:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(
                ok=False, error_code=MISSING_BINARY, stderr=f"{argv[0]} not found"
            )
        except PermissionError:
            return CommandResult(
                ok=False,
                error_code=PERMISSION_DENIED,
                stderr=f"permission denied: {argv[0]}",
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                ok=False, error_code=TIMEOUT, stderr=f"timed out after {timeout}s"
            )
        return CommandResult(
            ok=completed.returncode == 0,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            error_code="" if completed.returncode == 0 else FAILED,
        )


class DryRunCommandRunner:
    """Executes read-only commands, records mutating ones without running them.

    A command is treated as read-only when its argv matches one of the
    ``read_only_prefixes``. Everything else is recorded in ``recorded`` and
    reported as a successful no-op carrying ``DRY_RUN``, so the caller can
    exercise its full path without touching the host.
    """

    def __init__(
        self,
        inner: CommandRunner | None = None,
        *,
        read_only_prefixes: tuple[tuple[str, ...], ...] = (),
    ) -> None:
        self._inner = inner or RealCommandRunner()
        self._read_only_prefixes = read_only_prefixes
        self.recorded: list[list[str]] = []

    def _is_read_only(self, argv: list[str]) -> bool:
        return any(
            tuple(argv[: len(prefix)]) == prefix for prefix in self._read_only_prefixes
        )

    def run(self, argv: list[str], *, timeout: float = 5.0) -> CommandResult:
        if self._is_read_only(argv):
            return self._inner.run(argv, timeout=timeout)
        self.recorded.append(list(argv))
        return CommandResult(ok=True, stdout="", error_code=DRY_RUN)


class RecordingCommandRunner:
    """Test double: returns canned results and records every argv."""

    def __init__(
        self, responses: dict[tuple[str, ...], CommandResult] | None = None
    ) -> None:
        self._responses = responses or {}
        self.recorded: list[list[str]] = []

    def run(self, argv: list[str], *, timeout: float = 5.0) -> CommandResult:
        self.recorded.append(list(argv))
        for prefix, result in self._responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return result
        return CommandResult(ok=True)
