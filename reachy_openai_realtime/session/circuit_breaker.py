# ABOUTME: Fixed safety policy for bounding rapid false user turns.
# ABOUTME: Uses monotonic timestamps only; transcript content cannot reset it.

from __future__ import annotations

from collections import deque
from typing import Final

TURN_RATE_LIMIT: Final[int] = 5
TURN_RATE_WINDOW_SECONDS: Final[float] = 60.0
SESSION_LIMIT_SECONDS: Final[float] = 30 * 60.0


class TurnRateCircuitBreaker:
    def __init__(self) -> None:
        self._turns: deque[float] = deque()

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    def record_turn(self, now: float) -> bool:
        cutoff = now - TURN_RATE_WINDOW_SECONDS
        while self._turns and self._turns[0] <= cutoff:
            self._turns.popleft()
        self._turns.append(now)
        return len(self._turns) >= TURN_RATE_LIMIT
