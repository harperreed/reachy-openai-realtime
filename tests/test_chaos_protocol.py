# ABOUTME: Scripted-event protocol tests for _event_loop and _watchdog_loop.
# ABOUTME: Verifies idempotency guards and watchdog deadline firing without real I/O.
import asyncio
import time
from types import SimpleNamespace

import pytest

from conftest import ScriptedConnection, drive_fsm, realtime_event

from reachy_openai_realtime.audio.playback import PlaybackBuffer
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog, WatchdogTimeout
from reachy_openai_realtime.vad import EnergyTurnDetector
from test_realtime_manual_turn import BargeInMotion, FakeStopEvent


def test_duplicate_response_done_flushes_tool_outputs_once() -> None:
    done = realtime_event(
        "response.done",
        response=SimpleNamespace(id="resp_dup", status="completed", usage=None, output=[]),
    )
    connection = ScriptedConnection([done, done])
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    # Attributes below mirror what the response.done path reads (realtime.py:708-756
    # pre-rewrite): config/_language_provider feed _flush_tool_outputs' instructions,
    # motion/_vad/_speaker_busy_until feed the no-more-audio branch. If the rewrite
    # added a read this misses, pytest's AttributeError names it — add it with the
    # neutral value used in tests/test_realtime_reset.py's dirty-session builder.
    session.status = RuntimeStatus()
    session.connection = connection
    session.config = AppConfig()
    session._language_provider = None
    session.motion = BargeInMotion()
    session._vad = EnergyTurnDetector()
    session.fsm = SessionStateMachine()
    drive_fsm(session.fsm, SessionState.WAITING_RESPONSE)
    session.watchdog = DeadlineWatchdog()
    session.connection_epoch = 1
    session._response_generation_done = False
    session._current_response_id = "resp_dup"
    session._interrupted_response_ids = RecentIds()
    session._pending_tool_outputs = [(1, "call_1", '{"ok": true}')]
    session._playback = PlaybackBuffer()
    session._speaker_busy_until = time.monotonic() - 1.0
    session._current_audio_item_id = None
    session._current_audio_content_index = 0
    # Task-11 attributes the event loop also reads
    session._speech_ended_at = None
    session._barge_in_at = None
    session._first_write_pending = False

    async def run_event_loop() -> None:
        # After both scripted events the connection idles forever; cancel it.
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_event_loop())

    tool_items = [
        kwargs
        for kwargs in connection.conversation.item.created
        if kwargs.get("item", {}).get("type") == "function_call_output"
    ]
    assert len(tool_items) == 1
    assert len(connection.response.created) == 1


def test_session_updated_timeout_tears_down_connection() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.now = 10.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.watchdog = DeadlineWatchdog(clock=clock)
    session.watchdog.arm("session_update")

    async def advance_clock() -> None:
        await asyncio.sleep(0.05)
        clock.now += 60.0

    async def run() -> None:
        advancer = asyncio.ensure_future(advance_clock())
        try:
            await session._watchdog_loop()
        finally:
            advancer.cancel()

    with pytest.raises(WatchdogTimeout) as excinfo:
        asyncio.run(run())
    assert excinfo.value.operation == "session_update"
