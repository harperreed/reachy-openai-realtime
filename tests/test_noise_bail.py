# ABOUTME: Anti-runaway backstop tests — the wall-clock ceiling (hard guarantee)
# ABOUTME: and the wordless-transcript counter (fast bail). Both end in stop->sleep.
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import ScriptedConnection, drive_fsm

from reachy_openai_realtime import realtime as realtime_mod
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession, _is_garbage_transcript
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.supervisor import FSM_INACTIVITY_LIMIT_SECONDS
from reachy_openai_realtime.session.watchdog import WatchdogTimeout


@pytest.mark.parametrize("transcript", ["", "   ", "...", "!?!", "—", "  .. ! "])
def test_garbage_transcript_flags_wordless_turns(transcript):
    # A committed turn that transcribes to no word characters is noise, not speech.
    assert _is_garbage_transcript(transcript) is True


@pytest.mark.parametrize("transcript", ["yes", "ok", "mm-hmm", "助けて", "好", "k"])
def test_garbage_transcript_spares_real_words(transcript):
    # Any word character means real speech. \w is Unicode, so CJK counts, and a
    # lone real letter is spared — the wall-clock ceiling backstops the residue
    # rather than risk misreading short non-Latin words as noise.
    assert _is_garbage_transcript(transcript) is False


def _transcript_session(**config_kwargs) -> RealtimeRobotSession:
    """Minimal session exercising just the noise counter."""
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig(**config_kwargs)
    session.status = RuntimeStatus()
    session._noise_turn_count = 0
    return session


def test_noise_counter_increments_on_garbage():
    session = _transcript_session(noise_bail_turns=3)
    stop = threading.Event()
    session._note_user_transcript("", stop)
    session._note_user_transcript("...", stop)
    assert session._noise_turn_count == 2
    assert not stop.is_set()


def test_noise_counter_resets_on_real_turn():
    session = _transcript_session(noise_bail_turns=3)
    stop = threading.Event()
    session._note_user_transcript("", stop)
    session._note_user_transcript("hello there", stop)
    assert session._noise_turn_count == 0
    session._note_user_transcript("", stop)  # counts fresh, no carryover
    assert session._noise_turn_count == 1
    assert not stop.is_set()


def test_noise_counter_bails_to_sleep_at_threshold():
    session = _transcript_session(noise_bail_turns=3)
    stop = threading.Event()
    for _ in range(3):
        session._note_user_transcript("", stop)
    assert session._noise_turn_count == 3
    assert stop.is_set()


def test_noise_counter_disabled_when_turns_zero():
    session = _transcript_session(noise_bail_turns=0)
    stop = threading.Event()
    for _ in range(10):
        session._note_user_transcript("", stop)
    assert session._noise_turn_count == 0
    assert not stop.is_set()


class _EndOfScript(Exception):
    """Sentinel that ends _event_loop deterministically after the scripted event."""


def test_event_loop_routes_user_transcription_to_noise_bail():
    # Wiring: a committed-turn transcript event must reach the noise counter and
    # land as the user transcript in status. _note_user_transcript is tested
    # above; this proves the dispatch chain routes the event to it.
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig(noise_bail_turns=3)
    session.status = RuntimeStatus()
    session._noise_turn_count = 0
    event = SimpleNamespace(
        type="conversation.item.input_audio_transcription.completed",
        transcript="...",
    )
    session.connection = ScriptedConnection([event], raise_after=_EndOfScript())
    stop = threading.Event()

    with pytest.raises(_EndOfScript):
        asyncio.run(session._event_loop(stop))

    assert session._noise_turn_count == 1
    assert session.status.snapshot()["last_user"] == "..."


def _supervisor_session(**config_kwargs) -> RealtimeRobotSession:
    """Minimal session exercising just what _supervisor_loop reads."""
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig(**config_kwargs)
    session.status = RuntimeStatus()
    session.fsm = SessionStateMachine()
    session._last_fsm_transition_at = time.monotonic()  # fresh -> FSM-inactivity dormant
    session._session_started_at = None
    return session


async def _run_supervisor_until_stop(session, stop_event, *, timeout=2.0) -> None:
    task = asyncio.create_task(session._supervisor_loop(stop_event))
    await asyncio.wait_for(task, timeout=timeout)


async def _run_supervisor_briefly(session, stop_event, *, seconds=0.1) -> None:
    task = asyncio.create_task(session._supervisor_loop(stop_event))
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_wall_clock_ceiling_bails_to_sleep(monkeypatch):
    # The hard guarantee: a session older than the limit sets the stop flag, which
    # run()'s loop turns into SessionOutcome.STOPPED -> sleep. No classification.
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session(noise_bail_session_minutes=30)
    session._session_started_at = time.monotonic() - (30 * 60 + 1)  # just over the limit
    stop_event = threading.Event()

    asyncio.run(_run_supervisor_until_stop(session, stop_event))

    assert stop_event.is_set()


def test_wall_clock_ceiling_does_not_bail_before_limit(monkeypatch):
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session(noise_bail_session_minutes=30)
    session._session_started_at = time.monotonic() - 60  # one minute in, far from 30
    stop_event = threading.Event()

    asyncio.run(_run_supervisor_briefly(session, stop_event))

    assert not stop_event.is_set()


def test_wall_clock_ceiling_disabled_when_minutes_zero(monkeypatch):
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session(noise_bail_session_minutes=0)
    session._session_started_at = time.monotonic() - (999 * 60)  # ancient, still no bail
    stop_event = threading.Event()

    asyncio.run(_run_supervisor_briefly(session, stop_event))

    assert not stop_event.is_set()


def test_supervisor_still_raises_on_fsm_inactivity(monkeypatch):
    # Regression: adding the wall-clock branch must not disturb the existing
    # FSM-inactivity teardown, which RAISES (reconnect path), never sets stop.
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session(noise_bail_session_minutes=0)  # wall clock off
    session._session_started_at = time.monotonic()
    drive_fsm(session.fsm, SessionState.WAITING_RESPONSE)  # non-LISTENING
    session._last_fsm_transition_at = time.monotonic() - (FSM_INACTIVITY_LIMIT_SECONDS + 1)
    stop_event = threading.Event()

    with pytest.raises(WatchdogTimeout):
        asyncio.run(_run_supervisor_until_stop(session, stop_event))

    assert not stop_event.is_set()
