# ABOUTME: Shared test fixtures/helpers. drive_fsm walks a session state machine
# ABOUTME: to a target state through legal transitions only (exercises the table).
from collections import deque

from reachy_openai_realtime.session.fsm import LEGAL_TRANSITIONS, SessionState, SessionStateMachine


def drive_fsm(fsm: SessionStateMachine, target: SessionState) -> None:
    """Walk fsm to target via a shortest legal path. Raises if unreachable."""
    if fsm.state is target:
        return
    frontier = deque([(fsm.state, [])])
    seen = {fsm.state}
    while frontier:
        state, path = frontier.popleft()
        for nxt in LEGAL_TRANSITIONS[state]:
            if nxt in seen:
                continue
            if nxt is target:
                for step in path + [nxt]:
                    assert fsm.transition(step, reason="test_drive")
                return
            seen.add(nxt)
            frontier.append((nxt, path + [nxt]))
    raise AssertionError(f"no legal path from {fsm.state} to {target}")
