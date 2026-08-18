# ABOUTME: Explicit session state machine (spec §3). One transition() entry
# ABOUTME: point; illegal transitions warn (or assert in strict/test mode).
from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Callable

logger = logging.getLogger(__name__)


class SessionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    INITIALIZING = auto()
    LISTENING = auto()
    USER_SPEAKING = auto()
    WAITING_RESPONSE = auto()
    ASSISTANT_SPEAKING = auto()
    INTERRUPTING = auto()
    TOOL_EXECUTION = auto()
    RECOVERING = auto()
    STOPPING = auto()


_ALWAYS = frozenset({SessionState.RECOVERING, SessionState.STOPPING})

LEGAL_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.DISCONNECTED: frozenset({SessionState.CONNECTING}) | _ALWAYS,
    SessionState.CONNECTING: frozenset({SessionState.INITIALIZING}) | _ALWAYS,
    SessionState.INITIALIZING: frozenset({SessionState.LISTENING}) | _ALWAYS,
    SessionState.LISTENING: frozenset({SessionState.USER_SPEAKING, SessionState.WAITING_RESPONSE}) | _ALWAYS,
    SessionState.USER_SPEAKING: frozenset({SessionState.WAITING_RESPONSE, SessionState.LISTENING}) | _ALWAYS,
    SessionState.WAITING_RESPONSE: frozenset(
        {SessionState.ASSISTANT_SPEAKING, SessionState.TOOL_EXECUTION, SessionState.LISTENING}
    )
    | _ALWAYS,
    SessionState.ASSISTANT_SPEAKING: frozenset(
        {SessionState.INTERRUPTING, SessionState.LISTENING, SessionState.TOOL_EXECUTION}
    )
    | _ALWAYS,
    SessionState.INTERRUPTING: frozenset({SessionState.USER_SPEAKING, SessionState.LISTENING}) | _ALWAYS,
    SessionState.TOOL_EXECUTION: frozenset({SessionState.WAITING_RESPONSE, SessionState.LISTENING}) | _ALWAYS,
    SessionState.RECOVERING: frozenset(
        {SessionState.CONNECTING, SessionState.DISCONNECTED, SessionState.STOPPING}
    ),
    SessionState.STOPPING: frozenset({SessionState.DISCONNECTED}),
}

_ACCEPTS_USER_AUDIO = frozenset(
    {
        SessionState.LISTENING,
        SessionState.USER_SPEAKING,
        SessionState.ASSISTANT_SPEAKING,
        SessionState.INTERRUPTING,
    }
)

_GENERATION_ACTIVE = frozenset(
    {SessionState.WAITING_RESPONSE, SessionState.TOOL_EXECUTION, SessionState.ASSISTANT_SPEAKING}
)


class SessionStateMachine:
    """Control-truth for the Realtime session. UI phases stay presentation-only."""

    def __init__(
        self,
        *,
        on_transition: Callable[[SessionState, SessionState, str], None] | None = None,
        strict: bool = False,
    ) -> None:
        self._state = SessionState.DISCONNECTED
        self._on_transition = on_transition
        self._strict = strict

    @property
    def state(self) -> SessionState:
        return self._state

    def transition(self, new_state: SessionState, *, reason: str) -> bool:
        old_state = self._state
        if new_state is old_state:
            return True
        if new_state not in LEGAL_TRANSITIONS[old_state]:
            message = f"illegal transition {old_state.name} -> {new_state.name} ({reason})"
            if self._strict:
                raise AssertionError(message)
            logger.warning(message)
            return False
        self._state = new_state
        if self._on_transition is not None:
            try:
                self._on_transition(old_state, new_state, reason)
            except Exception:
                logger.exception("fsm transition listener failed")
        return True

    def accepts_user_audio(self) -> bool:
        return self._state in _ACCEPTS_USER_AUDIO

    def generation_active(self) -> bool:
        return self._state in _GENERATION_ACTIVE
