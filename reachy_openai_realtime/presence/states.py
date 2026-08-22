# ABOUTME: Presence state machine — legal transitions for Reachy's sleep/wake
# ABOUTME: lifecycle. Thread-safe: transitions arrive from API threads and the
# ABOUTME: presence control thread (issue #12 spec §5).
from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum, auto


class PresenceState(Enum):
    BOOTING = auto()
    SLEEPING = auto()
    WAKING = auto()
    AWAKE = auto()
    ERROR = auto()


# Directed edges the machine allows, beyond idempotent self-transitions.
_LEGAL_TRANSITIONS: dict[PresenceState, frozenset[PresenceState]] = {
    PresenceState.BOOTING: frozenset({PresenceState.SLEEPING}),
    PresenceState.SLEEPING: frozenset({PresenceState.WAKING, PresenceState.ERROR}),
    PresenceState.WAKING: frozenset({PresenceState.AWAKE, PresenceState.SLEEPING, PresenceState.ERROR}),
    PresenceState.AWAKE: frozenset({PresenceState.SLEEPING}),
    PresenceState.ERROR: frozenset({PresenceState.WAKING}),
}


class PresenceStateMachine:
    """Guards Reachy's presence lifecycle. Illegal edges raise; self-edges are
    idempotent no-ops that still fire the callback so observers can re-render."""

    def __init__(
        self,
        *,
        on_transition: Callable[[PresenceState, PresenceState, str], None] | None = None,
    ) -> None:
        self._state = PresenceState.BOOTING
        self._on_transition = on_transition
        self._lock = threading.Lock()

    @property
    def state(self) -> PresenceState:
        with self._lock:
            return self._state

    def transition(self, new_state: PresenceState, *, reason: str) -> None:
        with self._lock:
            old_state = self._state
            if new_state is not old_state and new_state not in _LEGAL_TRANSITIONS.get(
                old_state, frozenset()
            ):
                raise ValueError(
                    f"illegal presence transition {old_state.name} -> {new_state.name} ({reason})"
                )
            self._state = new_state
        if self._on_transition is not None:
            self._on_transition(old_state, new_state, reason)
