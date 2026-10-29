"""StatusHandler tests: the telemetry it returns must carry the worker identity.

The handler reads the identity off ``TaskHandlerContext``, which the framework
renamed from ``worker_id`` to ``instance_id`` in v4. A stale attribute name only
surfaces at task time, so the handler is driven here through the real context
type rather than a stand-in.
"""

import logging
import uuid
from pathlib import Path

import msgspec
import pytest
from scietex.service.task_handler import TaskCapabilities, TaskData, TaskHandlerContext

from scietex.system_time_service.config import SystemTimeSettings
from scietex.system_time_service.task_handlers import STATUS_TASK, StatusHandler
from tests.helpers import _runner, _runtime

INSTANCE_ID = "183791399ae14130ac87276fb107ad6e"


def _context() -> TaskHandlerContext:
    return TaskHandlerContext(
        service_name="SystemTime",
        instance_id=INSTANCE_ID,
        logger=logging.getLogger("test"),
    )


async def _no_progress(task_id: uuid.UUID, fraction: float) -> None:
    return None


@pytest.mark.asyncio
async def test_status_handler_reports_the_context_instance_id(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        SystemTimeSettings(timezone="Europe/Moscow", clock_source="auto"),
        _runner(),
    )
    handler = StatusHandler(STATUS_TASK, _context(), runtime=runtime)

    result = await handler.handle(
        TaskData(task_id="t", task=STATUS_TASK, payload=b""),
        capabilities=TaskCapabilities(
            task_id=uuid.uuid4(), _write_progress=_no_progress
        ),
    )

    assert result.status == "success"
    telemetry = msgspec.msgpack.decode(result.payload)
    assert telemetry["service"] == "SystemTime"
    assert telemetry["instance_id"] == INSTANCE_ID
