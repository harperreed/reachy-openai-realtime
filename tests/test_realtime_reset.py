# ABOUTME: Tests for RecentIds, connection_epoch, and reset_connection_state.
# ABOUTME: Verifies bounded ID tracking, stale-epoch filtering, and canonical state wipe.
import asyncio
import logging
import time

import numpy as np
import pytest
from conftest import FakeSpeakerMedia, ScriptedConnection, drive_fsm

from reachy_openai_realtime.audio.capture import AudioPipelineStalled, AudioRecoveryLadder, CaptureWorker
from reachy_openai_realtime.audio.playback import PlaybackBuffer, PlaybackChunk, SpeakerWorker
from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
from reachy_openai_realtime.tool_executor import ToolExecutor
from reachy_openai_realtime.vad import EnergyTurnDetector


def dirty_chunk() -> PlaybackChunk:
    return PlaybackChunk(
        epoch=3,
        response_id="resp_live",
        pcm=np.zeros(2400, dtype=np.int16),
        duration_ms=100.0,
        received_at=time.monotonic(),
    )


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
    session._playback = PlaybackBuffer()
    session._playback.push(dirty_chunk())
    session._speaker = SpeakerWorker(FakeSpeakerMedia())
    session._pending_tool_outputs = [(3, "call_1", "{}"), (2, "call_0", "{}")]
    session.tools = ToolExecutor(
        epoch_provider=lambda: session.connection_epoch,
        on_output=lambda *_: None,
        record_event=session.status.record_event,
    )
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
    session._camera_delete_events = {"del_evt_abc": "old_cam_item"}  # non-empty: seeded for reset assertions
    session._vad = EnergyTurnDetector()
    session._vad.speech_active = True
    wd = DeadlineWatchdog()
    wd.arm("response_create")
    session.watchdog = wd
    return session


def test_reset_connection_state_clears_spec_checklist() -> None:
    session = make_dirty_session()

    class FakeMotion:
        def stop_current(self) -> None:
            pass

    session.motion = FakeMotion()
    asyncio.run(session.reset_connection_state())

    assert session._playback.queued_ms() == 0.0
    assert session._pending_tool_outputs == []
    assert session._current_response_id is None
    assert session._current_audio_item_id is None
    assert session._playback_started_at is None
    assert session._playback_pushed_ms == 0.0
    assert session._speaker_busy_until <= time.monotonic()
    assert len(session._interrupted_response_ids) == 0
    assert session._pending_camera_items == {}
    assert session._camera_add_events == {}
    assert session._camera_delete_events == {}  # was seeded non-empty; reset must clear it
    assert session._last_camera_item_id is None
    assert session._response_generation_done is True
    assert session._vad.speech_active is False
    assert session.watchdog.expired() is None  # watchdog.clear() was called


def test_stale_epoch_tool_outputs_are_dropped_by_flush_filter() -> None:
    session = make_dirty_session()
    live = [
        output
        for output in session._pending_tool_outputs
        if output[0] == session.connection_epoch
    ]
    assert live == [(3, "call_1", "{}")]


# ---------------------------------------------------------------------------
# A3: _flush_tool_outputs all-stale guard
# ---------------------------------------------------------------------------


def make_flush_session() -> RealtimeRobotSession:
    """Minimal session wired to test _flush_tool_outputs."""
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.fsm = SessionStateMachine()
    drive_fsm(session.fsm, SessionState.TOOL_EXECUTION)
    session.connection = ScriptedConnection([])
    session.watchdog = DeadlineWatchdog()
    session.connection_epoch = 5
    session._language_provider = None
    session.config = type("Cfg", (), {"language": "en"})()
    session._pending_tool_outputs = []
    return session


def test_flush_tool_outputs_all_stale_returns_without_response_create() -> None:
    # If every pending output is from a prior epoch, _flush_tool_outputs must
    # return without sending response.create or transitioning the FSM.
    session = make_flush_session()
    # epoch 4 is stale relative to current epoch 5
    session._pending_tool_outputs = [(4, "call_stale", '{"ok":true}')]
    asyncio.run(session._flush_tool_outputs())

    assert session.connection.response.created == []
    # FSM must not have transitioned away from TOOL_EXECUTION
    assert session.fsm.state is SessionState.TOOL_EXECUTION


def test_flush_tool_outputs_live_epoch_sends_response_create() -> None:
    # Sanity: when at least one output is live the normal path runs.
    session = make_flush_session()
    session._pending_tool_outputs = [(5, "call_live", '{"ok":true}')]
    asyncio.run(session._flush_tool_outputs())

    assert len(session.connection.conversation.item.created) == 1
    assert len(session.connection.response.created) == 1
    assert session.fsm.state is SessionState.WAITING_RESPONSE


# ---------------------------------------------------------------------------
# A6: _playback_loop rejected-chunk accounting
# ---------------------------------------------------------------------------


def test_playback_loop_rejected_chunk_does_not_count_as_played() -> None:
    # When submit() returns accepted=False and stalled() is also False, the
    # chunk was silently dropped. record_audio_output_played must NOT fire.
    # Use a stub speaker whose submit() always returns False and stalled() always False.

    class _NeverAcceptSpeaker:
        """Stub speaker: always rejects submissions but never reports stalled."""
        last_write_at = time.monotonic()
        frames_total = 0

        def submit(self, *args, **kwargs) -> bool:
            return False

        def stalled(self, threshold_seconds: float) -> bool:
            return False

        def flush(self) -> None:
            pass

    class _FakeMotion:
        def set_speaking_enabled(self, enabled: bool) -> None:
            pass

    stop = type("Stop", (), {"is_set": lambda self: False})()

    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.motion = _FakeMotion()
    session.robot = type("Robot", (), {"media": type("M", (), {
        "get_output_audio_samplerate": lambda self: 24_000,
    })()})()
    session.config = type("Cfg", (), {"output_rate": 24_000})()
    session._playback = PlaybackBuffer()
    session._speaker = _NeverAcceptSpeaker()
    session._playback_io_lock = asyncio.Lock()
    session._playback_started_at = None
    session._playback_pushed_ms = 0.0
    session.connection_epoch = 1

    pcm_dummy = np.zeros(240, dtype=np.int16)
    chunk = PlaybackChunk(
        epoch=1,
        response_id="resp_a6",
        pcm=pcm_dummy,
        duration_ms=10.0,
        received_at=time.monotonic(),
    )
    session._playback.push(chunk)

    async def run_one_iteration():
        task = asyncio.create_task(session._playback_loop(stop))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_one_iteration())

    # The chunk was rejected (submit returned False, not stalled), so:
    assert session.status.snapshot()["audio_output_chunks_played"] == 0
    assert session._playback_pushed_ms == 0.0
    assert session._playback_started_at is None


# ---------------------------------------------------------------------------
# A8: barge_in_to_cancel_ms is only observed for real cancels
# ---------------------------------------------------------------------------


def test_barge_in_to_cancel_ms_not_recorded_when_generation_not_active() -> None:
    # When generation_active is False (no active response), _interrupt_assistant
    # must NOT record barge_in_to_cancel_ms (no cancel is issued).
    from conftest import FakeSpeakerMedia
    from test_realtime_manual_turn import BargeInMedia, BargeInMotion, FakeConnection, FakeStopEvent

    stop_event = FakeStopEvent()
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": BargeInMedia()})()
    session.motion = BargeInMotion()
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session.fsm = SessionStateMachine()
    # INTERRUPTING is legal only from ASSISTANT_SPEAKING, but _interrupt_assistant
    # checks generation_active() which is False for LISTENING.
    drive_fsm(session.fsm, SessionState.LISTENING)
    session.watchdog = DeadlineWatchdog()
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(FakeSpeakerMedia())
    session._playback_io_lock = asyncio.Lock()
    session._pending_tool_outputs = []
    session._response_generation_done = True  # generation NOT active
    session._speaker_busy_until = time.monotonic() + 5.0
    session._current_response_id = "resp_noop"
    session._current_audio_item_id = None
    session._current_audio_content_index = 0
    session._playback_started_at = None
    session._playback_pushed_ms = 0.0
    session._interrupted_response_ids = RecentIds()
    session.connection_epoch = 1
    session._speech_ended_at = None
    session._barge_in_at = None
    session.tools = ToolExecutor(
        epoch_provider=lambda: session.connection_epoch,
        on_output=lambda inv, result, output, ms: None,
        record_event=lambda *a, **kw: None,
    )

    asyncio.run(session._interrupt_assistant())

    latency = session.status.metrics.snapshot()["latency"]
    # barge_in_to_cancel_ms must not have been recorded — no cancel fired
    assert "barge_in_to_cancel_ms" not in latency
    # barge_in_to_silence_ms is still recorded (clear_playback always runs)
    assert "barge_in_to_silence_ms" in latency


# ---------------------------------------------------------------------------
# C1: _record_loop DoA poller fallback guard
# ---------------------------------------------------------------------------


def test_doa_poller_fallback_does_not_replace_existing_poller() -> None:
    # If _doa_poller is already set when _record_loop reaches the fallback site,
    # it must not create a second poller (which would leak the first thread).
    from test_realtime_manual_turn import FakeStopEvent

    class SentinelPoller:
        """Stand-in that tracks whether its start() was ever called."""

        def __init__(self) -> None:
            self.started = False

        def start(self) -> None:
            self.started = True

        def age_seconds(self) -> float:
            return float("inf")

        def latest(self, *, max_age_seconds: float = 0.6):
            return None

    class MediaWithDoA:
        def get_input_audio_samplerate(self) -> int:
            return 16_000

        def get_audio_sample(self):
            return None

        def get_DoA(self):
            return None

        def stop_recording(self) -> None:
            pass

        def start_recording(self) -> None:
            pass

    stop_event = FakeStopEvent()
    stop_event.stopped = True  # exit the record_loop immediately

    sentinel = SentinelPoller()
    media = MediaWithDoA()

    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": media})()
    session.motion = type("M", (), {
        "set_speaking_enabled": lambda self, v: None,
        "set_idle_enabled": lambda self, v: None,
        "set_listening_enabled": lambda self, v: None,
    })()
    session.config = type("Cfg", (), {"language": "en", "input_rate": 16_000})()
    session.status = RuntimeStatus()
    session.fsm = SessionStateMachine()
    session._response_generation_done = True
    session._playback = PlaybackBuffer()
    session._speaker_busy_until = time.monotonic() - 1.0
    session._vad = EnergyTurnDetector()
    session.watchdog = DeadlineWatchdog()
    session._mic_ladder = AudioRecoveryLadder()
    session._doa_poller = sentinel  # pre-set the sentinel
    session._connected_at = None
    session._capture = CaptureWorker(media)
    session._capture.start()
    session._audio = session._capture.subscribe("realtime")

    asyncio.run(session._record_loop(stop_event))
    session._capture.close()

    # The sentinel must still be the assigned poller — no second poller was created
    assert session._doa_poller is sentinel
    # The sentinel's start() must never have been called by the fallback
    assert not sentinel.started


# ---------------------------------------------------------------------------
# Bug A: _run_mic_recovery("restart_media") must hold _playback_io_lock
# ---------------------------------------------------------------------------


def _make_mic_recovery_session() -> RealtimeRobotSession:
    """Minimal session for testing _run_mic_recovery dispatch."""
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session._playback_io_lock = asyncio.Lock()
    session._mic_ladder = AudioRecoveryLadder()
    return session


def test_run_mic_recovery_restart_media_holds_lock() -> None:
    """restart_media must hold _playback_io_lock when calling _restart_media_pipeline.

    The Reachy Mini Wireless shares one GStreamer pipeline; concurrent restarts
    from the record loop and playback loop can corrupt it. This ensures the
    record-loop path matches the lock discipline of the other two callers.
    """
    session = _make_mic_recovery_session()

    lock_was_held = []

    def stub_restart_media_pipeline():
        lock_was_held.append(session._playback_io_lock.locked())

    session._restart_media_pipeline = stub_restart_media_pipeline
    session._restart_capture = lambda: None  # not under test here

    asyncio.run(session._run_mic_recovery("restart_media"))

    assert lock_was_held == [True], (
        "_restart_media_pipeline must be called while _playback_io_lock is held"
    )


def test_run_mic_recovery_restart_capture_calls_restart_capture() -> None:
    """restart_capture action must call _restart_capture (not _restart_media_pipeline)."""
    session = _make_mic_recovery_session()
    called = []
    session._restart_capture = lambda: called.append("capture")
    session._restart_media_pipeline = lambda: called.append("media")

    asyncio.run(session._run_mic_recovery("restart_capture"))

    assert called == ["capture"]


def test_run_mic_recovery_restart_session_raises() -> None:
    """restart_session must raise AudioPipelineStalled."""
    session = _make_mic_recovery_session()

    with pytest.raises(AudioPipelineStalled):
        asyncio.run(session._run_mic_recovery("restart_session"))


# ---------------------------------------------------------------------------
# _clear_playback: split exception handlers (issue #1 + comment fix from #5)
# ---------------------------------------------------------------------------


def _raise_runtime_error() -> None:
    raise RuntimeError("injected failure")


def _make_clear_playback_session() -> RealtimeRobotSession:
    """Minimal session for testing _clear_playback handler behaviour.

    Wires the real _playback, _speaker, and _playback_io_lock; media.audio
    exposes a working clear_player so tests can override just the piece under
    scrutiny.
    """

    class _ClearPlayerAudio:
        """Audio stub with a working clear_player (overrideable per-test)."""

        def clear_player(self) -> None:
            pass

    class _Media:
        def __init__(self) -> None:
            self.audio = _ClearPlayerAudio()
            self._start_recording_calls = 0

        def start_recording(self) -> None:
            self._start_recording_calls += 1

    media = _Media()
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": media})()
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(FakeSpeakerMedia())
    session._playback_io_lock = asyncio.Lock()
    return session


def test_clear_playback_logs_start_recording_failure_at_warning(caplog) -> None:
    """When start_recording raises after a flush, the failure must be visible at WARNING.

    The current code logs everything at DEBUG under 'clear_player failed', which
    means a dead mic after a barge-in leaves no trace at default log levels.
    """
    session = _make_clear_playback_session()
    session.robot.media.start_recording = _raise_runtime_error

    with caplog.at_level(logging.WARNING):
        asyncio.run(session._clear_playback())

    messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("start_recording failed" in m and "mic stall ladder" in m for m in messages)


def test_clear_playback_clear_player_failure_stays_debug_and_start_recording_still_runs(
    caplog,
) -> None:
    """When only clear_player fails: no WARNING fires, and start_recording still runs.

    The Wireless shares one GStreamer pipeline — start_recording must run even
    when clear_player raises, so the mic side is always reasserted.
    """
    session = _make_clear_playback_session()
    session.robot.media.audio.clear_player = _raise_runtime_error

    with caplog.at_level(logging.DEBUG):
        asyncio.run(session._clear_playback())

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("start_recording failed" in m for m in warning_messages), (
        "clear_player failure must not produce a WARNING"
    )
    # start_recording must still have been called despite the clear_player failure
    assert session.robot.media._start_recording_calls == 1
