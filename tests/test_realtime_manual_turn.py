import asyncio
import base64
import threading
import time
from types import SimpleNamespace

import numpy as np
from conftest import drive_fsm

from reachy_openai_realtime.audio.capture import AudioRecoveryLadder, CaptureWorker
from reachy_openai_realtime.audio.playback import PlaybackBuffer, SpeakerWorker
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import DoAPoller, RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
from reachy_openai_realtime.tool_executor import ToolExecutor
from reachy_openai_realtime.vad import EnergyTurnDetector


class FakeStopEvent:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped


class FakeInputAudioBuffer:
    def __init__(self, stop_event: FakeStopEvent) -> None:
        self.appended = 0
        self.committed = 0
        self.stop_event = stop_event

    async def append(self, *, audio: str) -> None:
        assert audio
        self.appended += 1

    async def commit(self) -> None:
        self.committed += 1


class FakeResponse:
    def __init__(self, stop_event: FakeStopEvent) -> None:
        self.created = 0
        self.cancelled: list[str | None] = []
        self.stop_event = stop_event

    async def create(self, response: dict[str, object] | None = None) -> None:
        self.created += 1
        self.last_response = response
        self.stop_event.stopped = True

    async def cancel(self, response_id: str | None = None) -> None:
        self.cancelled.append(response_id)


class FakeConversationItem:
    def __init__(self) -> None:
        self.truncations: list[dict[str, object]] = []
        self.created: list[dict[str, object]] = []
        self.deleted: list[str] = []

    async def create(self, **kwargs: object) -> None:
        self.created.append(kwargs)

    async def delete(self, *, item_id: str, **kwargs: object) -> None:
        self.deleted.append(item_id)

    async def truncate(self, **kwargs: object) -> None:
        self.truncations.append(kwargs)


class FakeConversation:
    def __init__(self) -> None:
        self.item = FakeConversationItem()


class FakeConnection:
    def __init__(self, stop_event: FakeStopEvent) -> None:
        self.input_audio_buffer = FakeInputAudioBuffer(stop_event)
        self.response = FakeResponse(stop_event)
        self.conversation = FakeConversation()


class FakeMedia:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = iter(frames)
        self.audio = type("Audio", (), {"clear_player": self._unexpected_clear})()

    @staticmethod
    def _unexpected_clear() -> None:
        raise AssertionError("local VAD must not flush the Wireless audio pipeline")

    def get_input_audio_samplerate(self) -> int:
        return 16_000

    def get_audio_sample(self) -> np.ndarray | None:
        return next(self.frames, None)

    def get_DoA(self) -> None:
        return None

    def stop_recording(self) -> None:
        pass

    def start_recording(self) -> None:
        pass

    def stop_playing(self) -> None:
        pass

    def start_playing(self) -> None:
        pass


class FakeMotion:
    def __init__(self) -> None:
        self.listening_states: list[bool] = []

    def stop_current(self) -> None:
        raise AssertionError("local VAD must not stop motion outside barge-in")

    def set_idle_enabled(self, enabled: bool) -> None:
        pass

    def set_listening_enabled(self, enabled: bool) -> None:
        self.listening_states.append(enabled)

    def set_speaking_enabled(self, enabled: bool) -> None:
        pass


class BargeInAudio:
    def __init__(self) -> None:
        self.cleared = 0

    def clear_player(self) -> None:
        self.cleared += 1


class BargeInMedia:
    def __init__(self) -> None:
        self.audio = BargeInAudio()
        self.recording_restarts = 0

    def start_recording(self) -> None:
        self.recording_restarts += 1


class BargeInMotion:
    def __init__(self) -> None:
        self.stopped = 0

    def stop_current(self, *, reason: str = "stop") -> None:
        self.stopped += 1

    def set_idle_enabled(self, enabled: bool) -> None:
        pass

    def set_listening_enabled(self, enabled: bool) -> None:
        pass

    def set_speaking_enabled(self, enabled: bool) -> None:
        pass

    def emotion_names(self) -> list:
        return []

    def dance_names(self) -> list:
        return []

    def tool_definitions(self) -> list:
        return []


class BargeInFramesMedia(BargeInMedia):
    def __init__(self, frames: list[np.ndarray]) -> None:
        super().__init__()
        self.frames = iter(frames)

    def get_input_audio_samplerate(self) -> int:
        return 16_000

    def get_audio_sample(self) -> np.ndarray | None:
        return next(self.frames, None)

    def get_DoA(self) -> tuple[float, bool]:
        return 0.0, True


def stereo_frame(dbfs: float) -> np.ndarray:
    amplitude = np.float32(10.0 ** (dbfs / 20.0))
    mono = np.full(320, amplitude, dtype=np.float32)
    return np.column_stack((mono, mono))


def test_doa_poller_never_blocks_caller_when_usb_read_stalls() -> None:
    release_read = threading.Event()
    stop_event = FakeStopEvent()
    poller = DoAPoller(lambda: release_read.wait(10.0) or None, stop_event)
    poller.start()

    started_at = time.monotonic()
    assert poller.latest() is None
    poller.close()
    assert time.monotonic() - started_at < 0.5
    release_read.set()


def test_record_loop_manually_commits_after_local_silence() -> None:
    stop_event = FakeStopEvent()
    frames = (
        [stereo_frame(-50.0) for _ in range(10)]
        + [stereo_frame(-30.0) for _ in range(15)]
        + [stereo_frame(-60.0) for _ in range(40)]
    )
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": FakeMedia(frames)})()
    session.motion = FakeMotion()
    session.config = AppConfig()
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(type("M", (), {"push_audio_sample": lambda self, d: None})())
    session.fsm = SessionStateMachine()
    session._response_generation_done = True
    drive_fsm(session.fsm, SessionState.LISTENING)
    session._speaker_busy_until = time.monotonic() - 1.0
    session._camera_enabled_callback = lambda: True
    session._capture_camera_jpeg = lambda: b"\xff\xd8camera-jpeg\xff\xd9"
    session._camera_capture_task = None
    session._last_camera_item_id = None
    session._pending_camera_items = {}
    session._camera_add_events = {}
    session._camera_delete_events = {}
    session._vad = EnergyTurnDetector()
    session.watchdog = DeadlineWatchdog()
    session._doa_poller = None
    session._connected_at = None

    session._capture = CaptureWorker(session.robot.media, max_buffer_ms=60_000.0)
    session._mic_ladder = AudioRecoveryLadder()
    session._capture.start()
    session._audio = session._capture.subscribe("realtime")
    asyncio.run(session._record_loop(stop_event))
    session._capture.close()

    assert session.connection.input_audio_buffer.appended > 0
    assert session.connection.input_audio_buffer.committed == 1
    assert session.connection.response.created == 1
    assert len(session.connection.conversation.item.created) == 1
    image_item = session.connection.conversation.item.created[0]["item"]
    assert image_item["type"] == "message"
    assert image_item["role"] == "user"
    assert image_item["content"][0]["type"] == "input_image"
    assert session.motion.listening_states == [True, False]
    assert session.status.snapshot()["phase"] == "thinking"
    instructions = session.connection.response.last_response["instructions"]
    assert "Reply only in natural English" in instructions


def test_camera_image_uses_data_uri_and_replaces_previous_image() -> None:
    stop_event = FakeStopEvent()
    jpeg = b"\xff\xd8speech-camera\xff\xd9"
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session._camera_enabled_callback = lambda: True
    session._capture_camera_jpeg = lambda: jpeg
    session._last_camera_item_id = None
    session._pending_camera_items = {}
    session._camera_add_events = {}
    session._camera_delete_events = {}
    session.watchdog = DeadlineWatchdog()

    assert asyncio.run(session._capture_and_send_camera_image()) is True
    first = session.connection.conversation.item.created[0]["item"]
    assert len(first["id"]) <= 32
    data_uri = first["content"][0]["image_url"]
    assert data_uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(data_uri.split(",", 1)[1]) == jpeg
    assert session.status.snapshot()["camera_images_sent"] == 0
    asyncio.run(
        session._confirm_camera_item(
            SimpleNamespace(item=SimpleNamespace(id=first["id"]))
        )
    )
    assert session.status.snapshot()["camera_images_sent"] == 1

    assert asyncio.run(session._capture_and_send_camera_image()) is True
    second = session.connection.conversation.item.created[1]["item"]
    asyncio.run(
        session._confirm_camera_item(
            SimpleNamespace(item=SimpleNamespace(id=second["id"]))
        )
    )
    assert session.connection.conversation.item.deleted == [first["id"]]
    status = session.status.snapshot()
    assert status["camera_images_sent"] == 2
    assert status["camera_bytes_sent"] == len(jpeg) * 2
    assert status["camera_send_errors"] == 0
    assert status["last_camera_image_at"] is not None


def test_camera_protocol_error_is_logged_without_marking_connection_failed() -> None:
    stop_event = FakeStopEvent()
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session._camera_enabled_callback = lambda: True
    session._capture_camera_jpeg = lambda: b"\xff\xd8camera\xff\xd9"
    session._last_camera_item_id = None
    session._pending_camera_items = {}
    session._camera_add_events = {}
    session._camera_delete_events = {}
    session.watchdog = DeadlineWatchdog()

    assert asyncio.run(session._capture_and_send_camera_image()) is True
    event_id = session.connection.conversation.item.created[0]["event_id"]
    handled = session._handle_camera_protocol_error(
        SimpleNamespace(event_id=event_id, message="invalid camera item")
    )

    assert handled is True
    status = session.status.snapshot()
    assert status["camera_send_errors"] == 1
    assert status["last_error"] is None


def test_barge_in_cancels_clears_and_truncates_at_played_audio() -> None:
    stop_event = FakeStopEvent()
    media = BargeInMedia()
    motion = BargeInMotion()
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": media})()
    session.motion = motion
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(type("M", (), {"push_audio_sample": lambda self, d: None})())
    session._playback_io_lock = asyncio.Lock()
    session._pending_tool_outputs = []
    session.connection_epoch = 1
    session.fsm = SessionStateMachine()
    session._response_generation_done = False
    drive_fsm(session.fsm, SessionState.ASSISTANT_SPEAKING)
    session._speaker_busy_until = time.monotonic() + 5.0
    session._current_response_id = "resp_123"
    session._current_audio_item_id = "item_123"
    session._current_audio_content_index = 0
    session._playback_started_at = time.monotonic() - 1.5
    session._playback_pushed_ms = 4_000.0
    session._interrupted_response_ids = RecentIds()
    session.watchdog = DeadlineWatchdog()
    session.tools = ToolExecutor(
        epoch_provider=lambda: session.connection_epoch,
        on_output=lambda inv, result, output, ms: None,
        record_event=lambda *a, **kw: None,
    )

    asyncio.run(session._interrupt_assistant())

    assert session.connection.response.cancelled == ["resp_123"]
    assert media.audio.cleared == 1
    assert media.recording_restarts == 1
    assert motion.stopped == 1
    truncation = session.connection.conversation.item.truncations[0]
    assert truncation["item_id"] == "item_123"
    assert 1_400 <= truncation["audio_end_ms"] <= 1_700
    assert session.status.snapshot()["interruptions"] == 1
    assert session.fsm.state is SessionState.USER_SPEAKING


def test_record_loop_detects_human_during_assistant_playback() -> None:
    stop_event = FakeStopEvent()
    frames = (
        [stereo_frame(-30.0) for _ in range(15)]
        + [stereo_frame(-60.0) for _ in range(40)]
    )
    media = BargeInFramesMedia(frames)
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": media})()
    session.motion = BargeInMotion()
    session.config = AppConfig()
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(type("M", (), {"push_audio_sample": lambda self, d: None})())
    session._playback_io_lock = asyncio.Lock()
    session._pending_tool_outputs = []
    session.connection_epoch = 1
    session.fsm = SessionStateMachine()
    session._response_generation_done = False
    drive_fsm(session.fsm, SessionState.ASSISTANT_SPEAKING)
    session._speaker_busy_until = time.monotonic() + 5.0
    session._current_response_id = "resp_playing"
    session._current_audio_item_id = "item_playing"
    session._current_audio_content_index = 0
    session._playback_started_at = time.monotonic() - 0.5
    session._playback_pushed_ms = 2_000.0
    session._interrupted_response_ids = RecentIds()
    session._vad = EnergyTurnDetector()
    session.watchdog = DeadlineWatchdog()
    session._doa_poller = None
    session._connected_at = None
    session.tools = ToolExecutor(
        epoch_provider=lambda: session.connection_epoch,
        on_output=lambda inv, result, output, ms: None,
        record_event=lambda *a, **kw: None,
    )

    session._capture = CaptureWorker(session.robot.media, max_buffer_ms=60_000.0)
    session._mic_ladder = AudioRecoveryLadder()
    session._capture.start()
    session._audio = session._capture.subscribe("realtime")
    asyncio.run(session._record_loop(stop_event))
    session._capture.close()

    assert session.connection.response.cancelled == ["resp_playing"]
    assert session.connection.input_audio_buffer.appended > 0
    assert session.connection.input_audio_buffer.committed == 1
    assert session.connection.response.created == 1
    assert session.status.snapshot()["interruptions"] == 1
