# ABOUTME: Scripted-event protocol tests for _event_loop and _watchdog_loop.
# ABOUTME: Verifies idempotency guards and watchdog deadline firing without real I/O.
import asyncio
import time
from types import SimpleNamespace

import pytest
from conftest import FakeRecorder, ScriptedConnection, drive_fsm, realtime_event
from test_realtime_manual_turn import BargeInMotion, FakeStopEvent

from reachy_openai_realtime.audio.playback import PlaybackBuffer
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog, WatchdogTimeout
from reachy_openai_realtime.tool_executor import ToolExecutor
from reachy_openai_realtime.vad import EnergyTurnDetector


def _build_response_done_session(connection: ScriptedConnection, response_id: str = "resp_dup") -> RealtimeRobotSession:
    """Construct a minimal session wired to `connection` ready to handle response.done.

    Attributes mirror what realtime.py's response.done branch reads (lines 890-910).
    If a new attribute is added upstream, pytest's AttributeError names it — add it
    here with the neutral value from tests/test_realtime_reset.py's dirty-session builder.
    """
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
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
    session._current_response_id = response_id
    session._interrupted_response_ids = RecentIds()
    session.tools = ToolExecutor(
        epoch_provider=lambda: session.connection_epoch,
        on_output=session._finish_tool_call,
        record_event=session.status.record_event,
    )
    session._pending_tool_outputs = [(1, "call_1", '{"ok": true}')]
    session._playback = PlaybackBuffer()
    session._speaker_busy_until = time.monotonic() - 1.0
    session._current_audio_item_id = None
    session._current_audio_content_index = 0
    # Task-11 attributes the event loop also reads
    session._speech_ended_at = None
    session._barge_in_at = None
    session._first_write_pending = False
    return session


def test_pending_tool_outputs_drained_once_on_repeated_response_done() -> None:
    """Two identical response.done events yield exactly one flush, not two.

    Mechanism: _flush_tool_outputs atomically swaps _pending_tool_outputs to []
    (realtime.py swap-and-drain: `pending, self._pending_tool_outputs = self._pending_tool_outputs, []`).
    The second response.done sees an empty list and takes the no-tool branch — no
    second flush, no second response.create.  RecentIds plays no role here.
    """
    done = realtime_event(
        "response.done",
        response=SimpleNamespace(id="resp_dup", status="completed", usage=None, output=[]),
    )
    drained = asyncio.Event()
    connection = ScriptedConnection([done, done], on_drained=lambda: drained.set())
    session = _build_response_done_session(connection, response_id="resp_dup")

    async def run_event_loop() -> None:
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        # Wait until the ScriptedConnection has yielded all scripted events.
        # on_drained fires after the last event is YIELDED (not yet fully handled),
        # so we give the handler loop a couple of zero-cost beats to finish processing
        # before we cancel.
        await asyncio.wait_for(drained.wait(), timeout=2.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
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


def test_interrupted_response_id_skips_flush() -> None:
    """A response.done whose id is in _interrupted_response_ids never flushes tool outputs.

    Mechanism: the was_interrupted guard (realtime.py ~line 890-906) short-circuits
    before reaching the _flush_tool_outputs branch.  Even with pending tool outputs,
    0 function_call_output items are created and 0 response.create calls are made.
    """
    response_id = "resp_interrupted"
    done = realtime_event(
        "response.done",
        response=SimpleNamespace(id=response_id, status="completed", usage=None, output=[]),
    )
    drained = asyncio.Event()
    connection = ScriptedConnection([done], on_drained=lambda: drained.set())
    session = _build_response_done_session(connection, response_id=response_id)
    # Seed the interrupted set — this is the mechanism under test.
    # The response.done handler will take the was_interrupted branch and skip flush.
    session._interrupted_response_ids.add(response_id)

    async def run_event_loop() -> None:
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        # Same choreography as the drain-once test: wait for all events to be yielded,
        # give the handler a couple of zero-cost turns to finish, then cancel.
        await asyncio.wait_for(drained.wait(), timeout=2.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_event_loop())

    tool_items = [
        kwargs
        for kwargs in connection.conversation.item.created
        if kwargs.get("item", {}).get("type") == "function_call_output"
    ]
    assert len(tool_items) == 0
    assert len(connection.response.created) == 0


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


# ---------------------------------------------------------------------------
# Bug C: FSM wedges in WAITING_RESPONSE after audio-free response.done
# ---------------------------------------------------------------------------


def test_audio_free_response_done_transitions_to_listening() -> None:
    """An audio-free, tool-free response.done must transition FSM to LISTENING.

    Before the fix, _assistant_audio_active() returned True while in
    WAITING_RESPONSE (because generation_active() includes that state), so
    the event handler took the else branch and never transitioned.
    """
    done = realtime_event(
        "response.done",
        response=SimpleNamespace(id="resp_audiofree", status="completed", usage=None, output=[]),
    )
    drained = asyncio.Event()
    connection = ScriptedConnection([done], on_drained=lambda: drained.set())

    session = _build_response_done_session(connection, response_id="resp_audiofree")
    # No pending tool outputs — pure audio-free, tool-free path
    session._pending_tool_outputs = []
    # Speaker idle — no audio was playing
    session._speaker_busy_until = float("-inf")

    async def run_event_loop() -> None:
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        await asyncio.wait_for(drained.wait(), timeout=2.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_event_loop())

    assert session.fsm.state is SessionState.LISTENING, (
        f"FSM must reach LISTENING after audio-free response.done; got {session.fsm.state}"
    )


def test_response_done_defers_listening_while_audio_still_playing() -> None:
    """response.done with speaker audio still draining must NOT transition to LISTENING.

    The other side of the audio_still_playing condition: an early transition would
    open the mic while the speaker plays, causing echo-triggered false barge-ins.
    The _record_loop rescue (ASSISTANT_SPEAKING + generation done + speaker idle)
    finishes the transition once playback drains.
    """
    done = realtime_event(
        "response.done",
        response=SimpleNamespace(id="resp_draining", status="completed", usage=None, output=[]),
    )
    drained = asyncio.Event()
    connection = ScriptedConnection([done], on_drained=lambda: drained.set())

    session = _build_response_done_session(connection, response_id="resp_draining")
    session._pending_tool_outputs = []
    drive_fsm(session.fsm, SessionState.ASSISTANT_SPEAKING)
    session._speaker_busy_until = time.monotonic() + 5.0

    async def run_event_loop() -> None:
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        await asyncio.wait_for(drained.wait(), timeout=2.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_event_loop())

    assert session.fsm.state is SessionState.ASSISTANT_SPEAKING, (
        f"FSM must stay ASSISTANT_SPEAKING while audio drains; got {session.fsm.state}"
    )
    assert session._response_generation_done is True


# ---------------------------------------------------------------------------
# Recorder assertions: response lifecycle events
# ---------------------------------------------------------------------------


def test_response_lifecycle_events_recorded() -> None:
    """response.created and response.completed fire with correct fields on response.done."""
    response_id = "resp_lifecycle"
    created_event = realtime_event(
        "response.created",
        response=SimpleNamespace(id=response_id),
    )
    done_event = realtime_event(
        "response.done",
        response=SimpleNamespace(id=response_id, status="completed", usage=None, output=[]),
    )
    drained = asyncio.Event()
    connection = ScriptedConnection([created_event, done_event], on_drained=lambda: drained.set())

    session = _build_response_done_session(connection, response_id=response_id)
    session._pending_tool_outputs = []
    session._speaker_busy_until = float("-inf")

    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)

    async def run_event_loop() -> None:
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        await asyncio.wait_for(drained.wait(), timeout=2.0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_event_loop())

    recorded_names = [e for e, _ in recorder.events]
    assert "response.created" in recorded_names, f"response.created not recorded; got {recorded_names}"
    assert "response.completed" in recorded_names, f"response.completed not recorded; got {recorded_names}"

    created_fields = next(f for e, f in recorder.events if e == "response.created")
    assert created_fields.get("response_id") == response_id

    completed_fields = next(f for e, f in recorder.events if e == "response.completed")
    assert completed_fields.get("response_id") == response_id
    assert isinstance(completed_fields.get("response_id"), str)
    assert completed_fields.get("status") == "completed"


class _SubmittingMotion(BargeInMotion):
    """Extends BargeInMotion with a submit() stub and tool_definitions() for the executor."""

    def submit(self, name: str, arguments: dict) -> dict:
        return {"ok": True}

    def tool_definitions(self) -> list:
        return [{"name": "set_emotion"}]


def test_tool_requested_and_completed_events_recorded() -> None:
    """tool.requested and tool.completed fire when _handle_tool_call dispatches async.

    After the ToolExecutor wiring, _handle_tool_call submits to the executor and
    returns immediately; the tool runs off-loop and _finish_tool_call delivers the
    result.  We wait until the executor is idle before asserting.
    """
    response_id = "resp_tool"
    tool_event = realtime_event(
        "response.function_call_arguments.done",
        response_id=response_id,
        call_id="call_tool_1",
        name="set_emotion",
        arguments='{"emotion": "happy"}',
    )
    drained = asyncio.Event()
    connection = ScriptedConnection([tool_event], on_drained=lambda: drained.set())

    session = _build_response_done_session(connection, response_id=response_id)
    motion = _SubmittingMotion()
    session.motion = motion
    session._pending_tool_outputs = []

    async def _set_emotion_handler(args):
        return await asyncio.to_thread(motion.submit, "set_emotion", args)

    # Register set_emotion so the executor can dispatch it (mirrors _register_motion_tools).
    session.tools.register(
        "set_emotion",
        _set_emotion_handler,
        timeout_s=10.0,
        category="motion",
    )

    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)

    async def run_event_loop() -> None:
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        await asyncio.wait_for(drained.wait(), timeout=2.0)
        # Drain async beats until the executor finishes the tool task.
        deadline = asyncio.get_running_loop().time() + 2.0
        while session.tools.busy():
            assert asyncio.get_running_loop().time() < deadline, "tool task never completed"
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_event_loop())

    recorded_names = [e for e, _ in recorder.events]
    assert "tool.requested" in recorded_names, f"tool.requested not recorded; got {recorded_names}"
    assert "tool.completed" in recorded_names, f"tool.completed not recorded; got {recorded_names}"

    tool_req = next(f for e, f in recorder.events if e == "tool.requested")
    assert tool_req.get("name") == "set_emotion"
    assert tool_req.get("call_id") == "call_tool_1"

    tool_done = next(f for e, f in recorder.events if e == "tool.completed")
    assert tool_done.get("name") == "set_emotion"
    assert tool_done.get("call_id") == "call_tool_1"
