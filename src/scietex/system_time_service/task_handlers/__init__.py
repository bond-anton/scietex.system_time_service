"""Task handlers.

Every handler shares the worker's ``ClockRuntime``, injected at registration
time. Handlers are thin: they validate the request, call the command layer, and
map the outcome to a ``TaskResult``. They never raise for an expected failure.
"""

import msgspec
from scietex.service.task_handler import (
    TaskCapabilities,
    TaskData,
    TaskHandler,
    TaskHandlerContext,
    TaskResult,
)

from ..clock import commands
from ..clock.runner import DRY_RUN, TIMEOUT
from ..runtime import ClockRuntime
from ..telemetry import build_telemetry

STATUS_TASK = "system_time:status"
READ_RTC_TASK = "system_time:read_rtc"
APPLY_TASK = "system_time:apply"
SET_CLOCK_TASK = "system_time:set_clock"

#: Error codes that a caller may usefully retry. Everything else is a
#: configuration or permission problem that a retry cannot fix.
RETRYABLE_CODES = frozenset({TIMEOUT})


class SetClockRequest(msgspec.Struct, frozen=True):
    epoch_seconds: float
    write_rtc: bool = True


def _result_from_command(result, *, payload: bytes = b"") -> TaskResult:
    if result.ok:
        return TaskResult(
            status="success", payload=payload, error_code=result.error_code
        )
    return TaskResult(
        status="error",
        error=result.stderr or result.error_code,
        error_code=result.error_code,
        retryable=result.error_code in RETRYABLE_CODES,
    )


class _RuntimeHandler(TaskHandler):
    """Base for handlers that need the shared clock runtime."""

    def __init__(
        self, name: str, context: TaskHandlerContext, *, runtime: ClockRuntime
    ) -> None:
        super().__init__(name, context)
        self.runtime = runtime


class StatusHandler(_RuntimeHandler):
    """Returns the current telemetry snapshot."""

    control = True

    @property
    def supported_tasks(self) -> list[str]:
        return [STATUS_TASK]

    async def handle(
        self, task_data: TaskData, *, capabilities: TaskCapabilities
    ) -> TaskResult:
        snapshot = self.runtime.collect()
        telemetry = build_telemetry(
            service=self.context.service_name,
            instance_id=self.context.worker_id,
            clock=snapshot.clock,
            ntp=snapshot.ntp,
            rtc=snapshot.rtc,
            battery=snapshot.battery,
            capabilities=self.runtime.capabilities,
            clock_source=self.runtime.settings.clock_source,
            pending_apply=self.runtime.pending_apply,
            last_error=self.runtime.last_error,
            last_rtc_sync_at=self.runtime.last_rtc_sync_at,
            last_rtc_sync_error=self.runtime.last_rtc_sync_error,
        )
        return TaskResult(status="success", payload=msgspec.msgpack.encode(telemetry))


class ReadRtcHandler(_RuntimeHandler):
    """Returns the RTC reading alone."""

    control = True

    @property
    def supported_tasks(self) -> list[str]:
        return [READ_RTC_TASK]

    async def handle(
        self, task_data: TaskData, *, capabilities: TaskCapabilities
    ) -> TaskResult:
        snapshot = self.runtime.collect()
        return TaskResult(
            status="success", payload=msgspec.msgpack.encode(snapshot.rtc)
        )


class ApplyHandler(_RuntimeHandler):
    """Re-applies the current settings to the host.

    The worker applies settings on config delivery already; this handler exists
    so the API can force convergence and learn whether it worked.
    """

    control = True

    @property
    def supported_tasks(self) -> list[str]:
        return [APPLY_TASK]

    async def handle(
        self, task_data: TaskData, *, capabilities: TaskCapabilities
    ) -> TaskResult:
        from ..system_time_worker import apply_settings

        result = apply_settings(self.runtime)
        self.runtime.pending_apply = not result.ok
        self.runtime.last_error = (
            None if result.ok else (result.stderr or result.error_code)
        )
        return _result_from_command(result)


class SetClockHandler(_RuntimeHandler):
    """Sets the system clock to an absolute instant."""

    control = True

    @property
    def supported_tasks(self) -> list[str]:
        return [SET_CLOCK_TASK]

    async def handle(
        self, task_data: TaskData, *, capabilities: TaskCapabilities
    ) -> TaskResult:
        try:
            request = msgspec.msgpack.decode(task_data.payload, type=SetClockRequest)
        except msgspec.DecodeError as exc:
            return TaskResult(
                status="error",
                error=f"invalid payload: {exc}",
                error_code="INVALID_PAYLOAD",
            )

        if self.runtime.settings.clock_source == "auto":
            return TaskResult(
                status="error",
                error="clock source is auto; switch to manual before setting the clock",
                error_code=commands.CLOCK_SOURCE_AUTO,
            )

        result = commands.set_clock(
            self.runtime.runner,
            self.runtime.capabilities,
            request.epoch_seconds,
            write_rtc=request.write_rtc,
            in_local_tz=self.runtime.settings.rtc_in_local_tz,
        )
        self.runtime.last_error = (
            None if result.ok else (result.stderr or result.error_code)
        )
        return _result_from_command(result)


__all__ = [
    "APPLY_TASK",
    "DRY_RUN",
    "READ_RTC_TASK",
    "SET_CLOCK_TASK",
    "STATUS_TASK",
    "ApplyHandler",
    "ReadRtcHandler",
    "SetClockHandler",
    "SetClockRequest",
    "StatusHandler",
]
