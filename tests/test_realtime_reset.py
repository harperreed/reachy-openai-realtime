# ABOUTME: Tests for RecentIds, connection_epoch, and reset_connection_state.
# ABOUTME: Verifies bounded ID tracking, stale-epoch filtering, and canonical state wipe.
import asyncio
import time

from conftest import drive_fsm

from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.vad import EnergyTurnDetector


def test_recent_ids_bounded() -> None:
    ids = RecentIds(max_size=3)
    for index in range(10):
        ids.add(f"resp_{index}")
    assert len(ids) == 3
    assert "resp_9" in ids
    assert "resp_0" not in ids
    ids.clear()
    assert len(ids) == 0


def make_dirty_session() -> RealtimeRobotSession:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.fsm = SessionStateMachine()
    drive_fsm(session.fsm, SessionState.ASSISTANT_SPEAKING)
    session.connection_epoch = 3
    session._response_generation_done = False
    session._playback_queue = asyncio.Queue()
    session._playback_queue.put_nowait(object())
    session._pending_tool_outputs = [(3, "call_1", "{}"), (2, "call_0", "{}")]
    session._current_response_id = "resp_live"
    session._current_audio_item_id = "item_live"
    session._current_audio_content_index = 1
    session._playback_started_at = time.monotonic()
    session._playback_pushed_ms = 1234.0
    session._speaker_busy_until = time.monotonic() + 9.0
    session._interrupted_response_ids = RecentIds()
    session._interrupted_response_ids.add("resp_old")
    session._camera_capture_task = None
    session._last_camera_item_id = "cam_item"
    session._pending_camera_items = {"evt": "cam_item"}
    session._camera_add_events = {"evt": "cam_item"}
    session._camera_delete_events = {}
    session._vad = EnergyTurnDetector()
    session._vad.speech_active = True
    return session


def test_reset_connection_state_clears_spec_checklist() -> None:
    session = make_dirty_session()

    class FakeMotion:
        def stop_current(self) -> None:
            pass

    session.motion = FakeMotion()
    asyncio.run(session.reset_connection_state())

    assert session._playback_queue.empty()
    assert session._pending_tool_outputs == []
    assert session._current_response_id is None
    assert session._current_audio_item_id is None
    assert session._playback_started_at is None
    assert session._playback_pushed_ms == 0.0
    assert session._speaker_busy_until <= time.monotonic()
    assert len(session._interrupted_response_ids) == 0
    assert session._pending_camera_items == {}
    assert session._camera_add_events == {}
    assert session._last_camera_item_id is None
    assert session._response_generation_done is True
    assert session._vad.speech_active is False


def test_stale_epoch_tool_outputs_are_dropped_by_flush_filter() -> None:
    session = make_dirty_session()
    live = [
        output
        for output in session._pending_tool_outputs
        if output[0] == session.connection_epoch
    ]
    assert live == [(3, "call_1", "{}")]
