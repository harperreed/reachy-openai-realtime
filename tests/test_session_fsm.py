# ABOUTME: Tests for the explicit session state machine in session/fsm.py.
# ABOUTME: Covers all states, transitions, strict mode, and query helpers.
import pytest

from reachy_openai_realtime.session.fsm import LEGAL_TRANSITIONS, SessionState, SessionStateMachine


def test_starts_disconnected() -> None:
    assert SessionStateMachine().state is SessionState.DISCONNECTED


def test_happy_path_conversation_cycle() -> None:
    transitions: list[tuple[SessionState, SessionState, str]] = []
    fsm = SessionStateMachine(on_transition=lambda old, new, reason: transitions.append((old, new, reason)))
    path = [
        (SessionState.CONNECTING, "socket_opening"),
        (SessionState.INITIALIZING, "socket_open"),
        (SessionState.LISTENING, "session_updated"),
        (SessionState.USER_SPEAKING, "vad_started"),
        (SessionState.WAITING_RESPONSE, "turn_committed"),
        (SessionState.ASSISTANT_SPEAKING, "first_audio_received"),
        (SessionState.LISTENING, "playback_finished"),
    ]
    for state, reason in path:
        assert fsm.transition(state, reason=reason) is True
    assert fsm.state is SessionState.LISTENING
    assert [entry[2] for entry in transitions] == [reason for _, reason in path]


def test_barge_in_path() -> None:
    fsm = SessionStateMachine()
    for state in [
        SessionState.CONNECTING,
        SessionState.INITIALIZING,
        SessionState.LISTENING,
        SessionState.WAITING_RESPONSE,
        SessionState.ASSISTANT_SPEAKING,
    ]:
        assert fsm.transition(state, reason="setup") is True
    assert fsm.transition(SessionState.INTERRUPTING, reason="barge_in") is True
    assert fsm.transition(SessionState.USER_SPEAKING, reason="vad_started") is True


def test_illegal_transition_returns_false_and_keeps_state() -> None:
    fsm = SessionStateMachine()
    assert fsm.transition(SessionState.ASSISTANT_SPEAKING, reason="nope") is False
    assert fsm.state is SessionState.DISCONNECTED


def test_illegal_transition_raises_in_strict_mode() -> None:
    fsm = SessionStateMachine(strict=True)
    with pytest.raises(AssertionError):
        fsm.transition(SessionState.ASSISTANT_SPEAKING, reason="nope")


def test_same_state_is_noop_without_listener_call() -> None:
    calls: list[str] = []
    fsm = SessionStateMachine(on_transition=lambda old, new, reason: calls.append(reason))
    fsm.transition(SessionState.CONNECTING, reason="first")
    assert fsm.transition(SessionState.CONNECTING, reason="again") is True
    assert calls == ["first"]


def test_any_active_state_may_recover_but_stopping_may_not() -> None:
    for state, allowed in LEGAL_TRANSITIONS.items():
        if state in (SessionState.STOPPING, SessionState.RECOVERING):
            continue
        assert SessionState.RECOVERING in allowed, state
        assert SessionState.STOPPING in allowed, state
    assert SessionState.RECOVERING not in LEGAL_TRANSITIONS[SessionState.STOPPING]
    assert LEGAL_TRANSITIONS[SessionState.STOPPING] == frozenset({SessionState.DISCONNECTED})


def test_query_helpers() -> None:
    fsm = SessionStateMachine()
    assert fsm.accepts_user_audio() is False
    for state in [SessionState.CONNECTING, SessionState.INITIALIZING, SessionState.LISTENING]:
        fsm.transition(state, reason="setup")
    assert fsm.accepts_user_audio() is True
    assert fsm.generation_active() is False
    fsm.transition(SessionState.WAITING_RESPONSE, reason="setup")
    assert fsm.generation_active() is True


def test_listener_exception_does_not_block_transition() -> None:
    def broken(old: SessionState, new: SessionState, reason: str) -> None:
        raise RuntimeError("listener bug")

    fsm = SessionStateMachine(on_transition=broken)
    assert fsm.transition(SessionState.CONNECTING, reason="setup") is True
    assert fsm.state is SessionState.CONNECTING
