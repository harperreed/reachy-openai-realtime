# ABOUTME: Supervisor policy for the self-healing ladder (hardening spec §24):
# ABOUTME: pure, clock-injected classes; wiring lives in realtime.py / main.py.
from __future__ import annotations

from collections import deque

SUPERVISOR_POLL_SECONDS = 5.0
FSM_INACTIVITY_LIMIT_SECONDS = 120.0
ESCALATION_PAUSE_SECONDS = 30.0


class RestartBudget:
    """Counts session rebuilds in a sliding window; True means escalate.

    Escalation = full media re-init + a longer pause, recorded as
    supervisor.escalated. Never an OS reboot (spec §24)."""

    def __init__(self, *, limit: int = 5, window_seconds: float = 600.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._restarts: deque[float] = deque()

    def record_restart(self, now: float) -> bool:
        self._restarts.append(now)
        while self._restarts and now - self._restarts[0] > self.window_seconds:
            self._restarts.popleft()
        return len(self._restarts) >= self.limit
