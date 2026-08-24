# ABOUTME: Anti-runaway backstop tests — the fixed wall-clock ceiling and
# ABOUTME: FSM-inactivity recovery remain independent stop and reconnect paths.
import asyncio
import threading
import time

import pytest
from conftest import drive_fsm

from reachy_openai_realtime import realtime as realtime_mod
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.circuit_breaker import SESSION_LIMIT_SECONDS
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.supervisor import FSM_INACTIVITY_LIMIT_SECONDS
from reachy_openai_realtime.session.watchdog import WatchdogTimeout


def _supervisor_session() -> RealtimeRobotSession:
    """Minimal session exercising just what _supervisor_loop reads."""
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session.status = RuntimeStatus()
    session.fsm = SessionStateMachine()
    session._last_fsm_transition_at = time.monotonic()  # fresh -> FSM-inactivity dormant
    session._session_started_at = None
    session._noise_bailed = False
    return session


async def _run_supervisor_until_stop(session, stop_event, *, timeout=2.0) -> None:
    task = asyncio.create_task(session._supervisor_loop(stop_event))
    await asyncio.wait_for(task, timeout=timeout)


async def _run_supervisor_briefly(session, stop_event, *, seconds=0.1) -> None:
    task = asyncio.create_task(session._supervisor_loop(stop_event))
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_wall_clock_ceiling_marks_noise_bail_and_stops(monkeypatch) -> None:
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session()
    session._session_started_at = time.monotonic() - (SESSION_LIMIT_SECONDS + 1)
    stop_event = threading.Event()

    asyncio.run(_run_supervisor_until_stop(session, stop_event))

    assert stop_event.is_set()
    assert session._noise_bailed is True


def test_wall_clock_ceiling_stays_idle_below_limit(monkeypatch) -> None:
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session()
    session._session_started_at = time.monotonic() - (SESSION_LIMIT_SECONDS - 1.0)
    stop_event = threading.Event()

    asyncio.run(_run_supervisor_briefly(session, stop_event))

    assert not stop_event.is_set()
    assert session._noise_bailed is False


def test_supervisor_still_raises_on_fsm_inactivity(monkeypatch):
    # Regression: adding the wall-clock branch must not disturb the existing
    # FSM-inactivity teardown, which RAISES (reconnect path), never sets stop.
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session()
    session._session_started_at = time.monotonic()
    drive_fsm(session.fsm, SessionState.WAITING_RESPONSE)  # non-LISTENING
    session._last_fsm_transition_at = time.monotonic() - (FSM_INACTIVITY_LIMIT_SECONDS + 1)
    stop_event = threading.Event()

    with pytest.raises(WatchdogTimeout):
        asyncio.run(_run_supervisor_until_stop(session, stop_event))

    assert not stop_event.is_set()
