# ABOUTME: Expectation-driven protocol deadlines (spec §5). A missed deadline
# ABOUTME: raises WatchdogTimeout, tearing down the connection for a clean rebuild.
from __future__ import annotations

import asyncio
import time
from typing import Callable

DEFAULT_DEADLINES: dict[str, float] = {
    "session_update": 5.0,
    "response_create": 5.0,
    "first_output": 15.0,
    "response_cancel": 3.0,
    "tool_response": 5.0,
    "input_append": 5.0,
    "camera_item": 5.0,
}


class WatchdogTimeout(RuntimeError):
    def __init__(self, operation: str, timeout_seconds: float) -> None:
        super().__init__(f"watchdog deadline expired: {operation} after {timeout_seconds:.1f}s")
        self.operation = operation
        self.timeout_seconds = timeout_seconds


class DeadlineWatchdog:
    """Tracks armed protocol deadlines against an injectable monotonic clock."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._deadlines: dict[str, tuple[float, float]] = {}  # op -> (deadline_at, timeout)

    def arm(self, operation: str, timeout_seconds: float | None = None) -> None:
        timeout = DEFAULT_DEADLINES[operation] if timeout_seconds is None else timeout_seconds
        self._deadlines[operation] = (self._clock() + timeout, timeout)

    def disarm(self, operation: str) -> None:
        self._deadlines.pop(operation, None)

    def clear(self) -> None:
        self._deadlines.clear()

    def expired(self) -> tuple[str, float] | None:
        now = self._clock()
        earliest: tuple[str, float] | None = None
        earliest_at = float("inf")
        for operation, (deadline_at, timeout) in self._deadlines.items():
            if deadline_at <= now and deadline_at < earliest_at:
                earliest = (operation, timeout)
                earliest_at = deadline_at
        return earliest

    async def watch(self, interval_seconds: float = 0.25) -> None:
        while True:
            hit = self.expired()
            if hit is not None:
                raise WatchdogTimeout(hit[0], hit[1])
            await asyncio.sleep(interval_seconds)
