# ABOUTME: Async tool executor (issue #21): runs model tool calls off the Realtime
# ABOUTME: receive loop with per-tool timeouts, epoch/duplicate guards, bounded concurrency.
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAX_PARALLEL_TOOLS = 2
MAX_RESULT_CHARS = 16_384
DEFAULT_TOOL_TIMEOUT_S = 15.0
MOTION_TOOL_TIMEOUT_S = 10.0
CAMERA_TOOL_TIMEOUT_S = 5.0  # reserved per issue #21; no camera tool registered today
TOOL_WATCHDOG_GRACE_S = 2.0

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
OutputCallback = Callable[["ToolInvocation", dict[str, Any], str, float], Awaitable[None]]


class RecentIds:
    """Bounded remembered-ID set (spec §27: interrupted response IDs must not grow forever)."""

    def __init__(self, max_size: int = 32) -> None:
        self._order: deque[str] = deque()
        self._members: set[str] = set()
        self._max_size = max_size

    def add(self, value: str) -> None:
        if value in self._members:
            return
        if len(self._order) >= self._max_size:
            self._members.discard(self._order.popleft())
        self._order.append(value)
        self._members.add(value)

    def __contains__(self, value: object) -> bool:
        return value in self._members

    def __len__(self) -> int:
        return len(self._order)

    def clear(self) -> None:
        self._order.clear()
        self._members.clear()


@dataclass(frozen=True)
class ToolInvocation:
    epoch: int
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class RegisteredTool:
    handler: ToolHandler
    timeout_s: float
    category: str  # names the timeout error: f"{category}_timeout"


class ToolExecutor:
    """Bounded async dispatch for model-invoked tools (issue #21).

    Every outcome — success, timeout, handler exception, guard drop — is a
    structured dict; nothing ever raises into the submitting (receive) loop.
    """

    def __init__(
        self,
        *,
        epoch_provider: Callable[[], int],
        on_output: OutputCallback,
        record_event: Callable[..., None],
        max_parallel: int = MAX_PARALLEL_TOOLS,
    ) -> None:
        self._epoch_provider = epoch_provider
        self._on_output = on_output
        self._record_event = record_event
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._tools: dict[str, RegisteredTool] = {}
        self._seen_call_ids = RecentIds()
        self._tasks: set[asyncio.Task[None]] = set()

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
        category: str = "other",
    ) -> None:
        self._tools[name] = RegisteredTool(handler=handler, timeout_s=timeout_s, category=category)

    def timeout_for(self, name: str) -> float:
        registered = self._tools.get(name)
        return DEFAULT_TOOL_TIMEOUT_S if registered is None else registered.timeout_s

    def busy(self) -> bool:
        return bool(self._tasks)

    async def submit(self, invocation: ToolInvocation) -> bool:
        if invocation.epoch != self._epoch_provider():
            self._record_event(
                "tool.failed", name=invocation.name, call_id=invocation.call_id, error="stale_epoch"
            )
            return False
        if invocation.call_id in self._seen_call_ids:
            self._record_event(
                "tool.failed", name=invocation.name, call_id=invocation.call_id, error="duplicate_call_id"
            )
            return False
        self._seen_call_ids.add(invocation.call_id)
        task = asyncio.create_task(
            self._run(invocation), name=f"tool-{invocation.name}-{invocation.call_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, invocation: ToolInvocation) -> None:
        async with self._semaphore:
            registered = self._tools.get(invocation.name)
            started = time.monotonic()
            if registered is None:
                result: dict[str, Any] = {"ok": False, "error": f"unknown tool: {invocation.name}"}
            else:
                try:
                    result = await asyncio.wait_for(
                        registered.handler(invocation.arguments), registered.timeout_s
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    result = {"ok": False, "error": f"{registered.category}_timeout"}
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc) or type(exc).__name__}
                if not isinstance(result, dict):
                    result = {"ok": False, "error": "tool returned a non-dict result"}
            duration_ms = (time.monotonic() - started) * 1000.0
            output = json.dumps(result, ensure_ascii=False)
            if len(output) > MAX_RESULT_CHARS:
                result = {"ok": False, "error": "result_too_large"}
                output = json.dumps(result, ensure_ascii=False)
            if invocation.epoch != self._epoch_provider():
                self._record_event(
                    "tool.failed", name=invocation.name, call_id=invocation.call_id, error="stale_epoch"
                )
                return
            # Drop ourselves from the in-flight set before delivering, so busy()
            # inside the callback reflects only OTHER tools (the session disarms
            # its watchdog when the last in-flight tool finishes).
            current = asyncio.current_task()
            if current is not None:
                self._tasks.discard(current)
            try:
                await self._on_output(invocation, result, output, duration_ms)
            except Exception:
                logger.exception("tool output callback failed: %s", invocation.name)

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
