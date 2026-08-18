# ABOUTME: Reconnect backoff policy and connection-error classification.
# ABOUTME: (spec §5): transient errors retry forever; config errors stop.
from __future__ import annotations

import random
from enum import Enum, auto


class BackoffPolicy:
    """Jittered exponential backoff, reset after a healthy connection."""

    DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

    def __init__(
        self,
        *,
        jitter_ratio: float = 0.2,
        healthy_reset_seconds: float = 60.0,
        rng: random.Random | None = None,
    ) -> None:
        self._jitter_ratio = jitter_ratio
        self._healthy_reset_seconds = healthy_reset_seconds
        self._rng = rng or random.Random()
        self._attempt = 0

    def note_session_duration(self, seconds: float) -> None:
        if seconds >= self._healthy_reset_seconds:
            self.reset()

    def next_delay(self) -> float:
        base = self.DELAYS[min(self._attempt, len(self.DELAYS) - 1)]
        self._attempt += 1
        if self._jitter_ratio <= 0.0:
            return base
        return base * self._rng.uniform(1.0 - self._jitter_ratio, 1.0 + self._jitter_ratio)

    def reset(self) -> None:
        self._attempt = 0


class ErrorClass(Enum):
    TRANSIENT = auto()
    FATAL_CONFIG = auto()


class SessionOutcome(Enum):
    STOPPED = auto()
    FATAL_CONFIG = auto()


_FATAL_EXCEPTION_NAMES = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "BadRequestError",
        "UnprocessableEntityError",
    }
)


def _find_status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    for attribute in ("status_code", "status"):
        status = getattr(response, attribute, None)
        if isinstance(status, int):
            return status
    return None


def classify_connection_error(exc: BaseException) -> ErrorClass:
    """Spec §5: auth/model/config failures must not cause reconnect spam.

    Matches by HTTP status where the exception exposes one (openai SDK and
    websocket handshake errors both do), falling back to SDK exception names.
    Unknown errors default to TRANSIENT — robustness beats giving up.
    """
    status = _find_status_code(exc)
    if status is not None:
        if status == 429:
            return ErrorClass.TRANSIENT
        if 400 <= status < 500:
            return ErrorClass.FATAL_CONFIG
        return ErrorClass.TRANSIENT
    if type(exc).__name__ in _FATAL_EXCEPTION_NAMES:
        return ErrorClass.FATAL_CONFIG
    return ErrorClass.TRANSIENT
