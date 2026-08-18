# ABOUTME: Tests for Task 11 latency and reliability metrics wiring in RealtimeRobotSession.
# ABOUTME: Covers speech-end latency, barge-in latency, speaker-write callback, and metric names.
import asyncio
import time

from conftest import drive_fsm

from reachy_openai_realtime.audio.playback import PlaybackBuffer, SpeakerWorker
from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
from test_audio_playback import FakeSpeakerMedia
from test_realtime_manual_turn import BargeInMedia, BargeInMotion, FakeConnection, FakeStopEvent


def test_observe_speech_latency_records_elapsed_ms() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session._speech_ended_at = time.monotonic() - 0.5
    session._observe_speech_latency("speech_end_to_response_created_ms")
    stat = session.status.metrics.snapshot()["latency"]["speech_end_to_response_created_ms"]
    assert stat["count"] == 1
    assert 400.0 <= stat["p50"] <= 1500.0


def test_observe_speech_latency_noop_without_timestamp() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session._speech_ended_at = None
    session._observe_speech_latency("speech_end_to_response_created_ms")
    assert session.status.metrics.snapshot()["latency"] == {}


def test_barge_in_records_cancel_and_silence_latency() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": BargeInMedia()})()
    session.motion = BargeInMotion()
    session.status = RuntimeStatus()
    session.connection = FakeConnection(FakeStopEvent())
    session.fsm = SessionStateMachine()
    drive_fsm(session.fsm, SessionState.ASSISTANT_SPEAKING)
    session.watchdog = DeadlineWatchdog()
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(FakeSpeakerMedia())
    session._playback_io_lock = asyncio.Lock()
    session._pending_tool_outputs = []
    session._response_generation_done = False
    session._speaker_busy_until = time.monotonic() + 5.0
    session._current_response_id = "resp_metrics"
    session._current_audio_item_id = "item_metrics"
    session._current_audio_content_index = 0
    session._playback_started_at = time.monotonic() - 1.0
    session._playback_pushed_ms = 2_000.0
    session._interrupted_response_ids = RecentIds()
    session.connection_epoch = 1
    session._speech_ended_at = None
    session._barge_in_at = None

    asyncio.run(session._interrupt_assistant())

    latency = session.status.metrics.snapshot()["latency"]
    assert latency["barge_in_to_cancel_ms"]["count"] == 1
    assert latency["barge_in_to_silence_ms"]["count"] == 1


def test_speaker_write_callback_records_first_audio_played() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session._speech_ended_at = time.monotonic() - 0.3
    session._first_write_pending = True
    session._on_speaker_write(20.0, time.monotonic() - 0.05)
    latency = session.status.metrics.snapshot()["latency"]
    assert latency["speech_end_to_first_audio_played_ms"]["count"] == 1
    assert latency["audio_receive_to_playback_ms"]["count"] == 1
    assert session._first_write_pending is False
    # subsequent writes only record receive→playback, not first-audio
    session._on_speaker_write(20.0, time.monotonic())
    latency = session.status.metrics.snapshot()["latency"]
    assert latency["speech_end_to_first_audio_played_ms"]["count"] == 1
    assert latency["audio_receive_to_playback_ms"]["count"] == 2
