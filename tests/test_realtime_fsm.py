# ABOUTME: FSM integration tests for RealtimeRobotSession. Verifies that the
# ABOUTME: record loop drives FSM state correctly during a manual turn.
import asyncio
import time

import numpy as np

from conftest import FakeSpeakerMedia, drive_fsm

from reachy_openai_realtime.audio.capture import AudioRecoveryLadder, CaptureWorker
from reachy_openai_realtime.audio.playback import PlaybackBuffer, SpeakerWorker
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
from reachy_openai_realtime.vad import EnergyTurnDetector

from test_realtime_manual_turn import FakeConnection, FakeMedia, FakeMotion, FakeStopEvent, stereo_frame


class ExhaustionStopMedia(FakeMedia):
    """FakeMedia that sets stop_event.stopped when frames run out."""

    def __init__(self, frames: list[np.ndarray], stop_event: FakeStopEvent) -> None:
        super().__init__(frames)
        self._stop_event = stop_event

    def get_audio_sample(self) -> np.ndarray | None:
        sample = super().get_audio_sample()
        if sample is None:
            self._stop_event.stopped = True
        return sample


def make_session(frames, stop_event) -> RealtimeRobotSession:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": FakeMedia(frames)})()
    session.motion = FakeMotion()
    session.config = AppConfig()
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session.fsm = SessionStateMachine()
    session._response_generation_done = True
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(FakeSpeakerMedia())
    session._speaker_busy_until = time.monotonic() - 1.0
    session._camera_enabled_callback = lambda: False
    session._camera_capture_task = None
    session._last_camera_item_id = None
    session._pending_camera_items = {}
    session._camera_add_events = {}
    session._camera_delete_events = {}
    session._vad = EnergyTurnDetector()
    session.watchdog = DeadlineWatchdog()
    return session


def test_manual_turn_walks_listening_speaking_waiting() -> None:
    stop_event = FakeStopEvent()
    frames = (
        [stereo_frame(-50.0) for _ in range(10)]
        + [stereo_frame(-30.0) for _ in range(15)]
        + [stereo_frame(-60.0) for _ in range(40)]
    )
    session = make_session(frames, stop_event)
    drive_fsm(session.fsm, SessionState.LISTENING)

    session._capture = CaptureWorker(session.robot.media, max_buffer_ms=60_000.0)
    session._mic_ladder = AudioRecoveryLadder()
    session._capture.start()
    asyncio.run(session._record_loop(stop_event))
    session._capture.close()

    assert session.connection.input_audio_buffer.committed == 1
    assert session.connection.response.created == 1
    assert session.fsm.state is SessionState.WAITING_RESPONSE
    assert session._response_generation_done is False


def test_frames_ignored_while_waiting_for_response() -> None:
    stop_event = FakeStopEvent()
    frames = [stereo_frame(-30.0) for _ in range(15)]
    session = make_session(frames, stop_event)
    session.robot = type("Robot", (), {"media": ExhaustionStopMedia(frames, stop_event)})()
    drive_fsm(session.fsm, SessionState.WAITING_RESPONSE)

    session._capture = CaptureWorker(session.robot.media, max_buffer_ms=60_000.0)
    session._mic_ladder = AudioRecoveryLadder()
    session._capture.start()
    asyncio.run(session._record_loop(stop_event))
    session._capture.close()

    assert session.connection.input_audio_buffer.appended == 0
    assert session.connection.input_audio_buffer.committed == 0
    assert session.fsm.state is SessionState.WAITING_RESPONSE
