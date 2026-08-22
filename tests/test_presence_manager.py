import pytest

from reachy_openai_realtime.presence.states import PresenceState, PresenceStateMachine


def test_starts_booting_and_boots_to_sleeping():
    fsm = PresenceStateMachine()
    assert fsm.state is PresenceState.BOOTING
    fsm.transition(PresenceState.SLEEPING, reason="boot_complete")
    assert fsm.state is PresenceState.SLEEPING


def test_wake_cycle_transitions_are_legal():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.WAKING, reason="wake_word")
    fsm.transition(PresenceState.AWAKE, reason="session_ready")
    fsm.transition(PresenceState.SLEEPING, reason="manual_sleep")


def test_startup_failure_returns_to_sleeping():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.WAKING, reason="wake_word")
    fsm.transition(PresenceState.SLEEPING, reason="startup_failure")


def test_illegal_transition_raises():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    with pytest.raises(ValueError, match="illegal presence transition"):
        fsm.transition(PresenceState.AWAKE, reason="skip_waking")


def test_self_transition_is_idempotent():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.SLEEPING, reason="already_asleep")
    assert fsm.state is PresenceState.SLEEPING


def test_on_transition_callback_fires_with_from_to_reason():
    seen: list[tuple] = []
    fsm = PresenceStateMachine(on_transition=lambda old, new, reason: seen.append((old, new, reason)))
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    assert seen == [(PresenceState.BOOTING, PresenceState.SLEEPING, "boot")]


def test_error_recovers_via_waking():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.ERROR, reason="model_download_failed")
    fsm.transition(PresenceState.WAKING, reason="manual_wake")
