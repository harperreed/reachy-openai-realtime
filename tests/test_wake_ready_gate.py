# ABOUTME: Wake startup data-flow tests for the local ready beep and input gate.
# ABOUTME: Covers discard ordering, stop/epoch races, and pre-ready reconnect suppression.

import asyncio
import threading
import time
from types import SimpleNamespace

import numpy as np
from conftest import FakeRecorder

from reachy_openai_realtime.audio.fanout import AudioFrame, AudioSubscription
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState
from reachy_openai_realtime.session.recovery import SessionOutcome


class GateMedia:
    def __init__(self) -> None:
        self.pushed: list[np.ndarray] = []

    def get_input_audio_samplerate(self) -> int:
        return 16_000

    def get_output_audio_samplerate(self) -> int:
        return 24_000

    def get_DoA(self) -> None:
        return None

    def push_audio_sample(self, data: np.ndarray) -> None:
        self.pushed.append(data)


class BlockingGateMedia(GateMedia):
    def __init__(self) -> None:
        super().__init__()
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def push_audio_sample(self, data: np.ndarray) -> None:
        self.write_started.set()
        self.release_write.wait(timeout=2.0)
        super().push_audio_sample(data)


class GateMotion:
    def tool_definitions(self) -> list[dict[str, object]]:
        return []

    def set_idle_enabled(self, enabled: bool) -> None:
        pass

    def set_listening_enabled(self, enabled: bool) -> None:
        pass

    def set_speaking_enabled(self, enabled: bool) -> None:
        pass


class CountingInputBuffer:
    def __init__(self) -> None:
        self.appended = 0
        self.committed = 0

    async def append(self, *, audio: str) -> None:
        self.appended += 1

    async def commit(self) -> None:
        self.committed += 1


class CountingResponse:
    def __init__(self) -> None:
        self.created = 0

    async def create(self, response=None) -> None:
        self.created += 1


def make_gate_session(monkeypatch, media: GateMedia, *, on_ready=None) -> RealtimeRobotSession:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-wake-ready-gate")
    robot = SimpleNamespace(media=media)
    return RealtimeRobotSession(
        robot=robot,
        motion=GateMotion(),
        config=AppConfig(),
        status=RuntimeStatus(),
        capture=SimpleNamespace(frame_age_seconds=lambda: 0.0),
        wake_session=True,
        on_session_ready=on_ready,
    )


def test_wake_session_discards_frames_while_input_gate_is_closed(monkeypatch) -> None:
    async def scenario() -> tuple[RealtimeRobotSession, CountingInputBuffer, CountingResponse]:
        session = make_gate_session(monkeypatch, GateMedia())
        subscription = AudioSubscription("realtime")
        session._audio = subscription
        input_buffer = CountingInputBuffer()
        response = CountingResponse()
        session.connection = SimpleNamespace(input_audio_buffer=input_buffer, response=response)
        stop_event = threading.Event()
        task = asyncio.create_task(session._record_loop(stop_event))
        subscription._offer(
            AudioFrame(
                samples=np.full(160, 20_000, dtype=np.int16),
                sample_rate=16_000,
                captured_at=time.monotonic(),
            )
        )
        deadline = time.monotonic() + 1.0
        while session._discarded_wake_frames == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)
        return session, input_buffer, response

    session, input_buffer, response = asyncio.run(scenario())
    assert session._discarded_wake_frames == 1
    assert session._vad.speech_active is False
    assert input_buffer.appended == 0
    assert input_buffer.committed == 0
    assert response.created == 0


def test_frame_captured_before_gate_stays_discarded_after_gate_opens(monkeypatch) -> None:
    async def scenario() -> RealtimeRobotSession:
        session = make_gate_session(monkeypatch, GateMedia())
        subscription = AudioSubscription("realtime")
        session._audio = subscription
        session.connection = SimpleNamespace(
            input_audio_buffer=CountingInputBuffer(),
            response=CountingResponse(),
        )
        captured_at = time.monotonic()
        session._input_ready_at = captured_at + 0.1
        stop_event = threading.Event()
        task = asyncio.create_task(session._record_loop(stop_event))
        subscription._offer(
            AudioFrame(
                samples=np.full(160, 20_000, dtype=np.int16),
                sample_rate=16_000,
                captured_at=captured_at,
            )
        )
        deadline = time.monotonic() + 1.0
        while session._discarded_wake_frames == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)
        return session

    session = asyncio.run(scenario())
    assert session._discarded_wake_frames == 1
    assert session._vad.speech_active is False


def test_ready_beep_opens_gate_before_ready_callback(monkeypatch) -> None:
    media = GateMedia()
    callback_gate_values: list[bool] = []
    session = make_gate_session(monkeypatch, media)
    session._on_session_ready = lambda: callback_gate_values.append(session.input_ready)
    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)
    session.connection_epoch = 1
    session.fsm.transition(SessionState.CONNECTING, reason="test")
    session.fsm.transition(SessionState.INITIALIZING, reason="test")
    session._speaker.start()
    try:

        async def scenario() -> None:
            configured = asyncio.Event()
            configured.set()
            await session._wake_readiness_loop(threading.Event(), configured, 1)

        asyncio.run(scenario())
    finally:
        session._speaker.close()

    names = [name for name, _fields in recorder.events]
    assert names.index("wake.ready_beep_started") < names.index("wake.ready_beep_completed")
    assert names.index("wake.ready_beep_completed") < names.index("wake.input_gate_opened")
    assert len(media.pushed) == 1
    assert media.pushed[0].dtype == np.float32
    assert session.input_ready is True
    assert session.fsm.state is SessionState.LISTENING
    assert callback_gate_values == [True]


def test_stop_during_ready_beep_never_opens_gate(monkeypatch) -> None:
    media = BlockingGateMedia()
    callback_count = 0

    def on_ready() -> None:
        nonlocal callback_count
        callback_count += 1

    session = make_gate_session(monkeypatch, media, on_ready=on_ready)
    session.connection_epoch = 1
    stop_event = threading.Event()
    session._speaker.start()
    try:

        async def scenario() -> None:
            configured = asyncio.Event()
            configured.set()
            task = asyncio.create_task(session._wake_readiness_loop(stop_event, configured, 1))
            assert await asyncio.to_thread(media.write_started.wait, 1.0)
            stop_event.set()
            media.release_write.set()
            await asyncio.wait_for(task, timeout=1.0)

        asyncio.run(scenario())
    finally:
        media.release_write.set()
        session._speaker.close()

    assert session.input_ready is False
    assert callback_count == 0


def test_stale_connection_epoch_never_opens_gate(monkeypatch) -> None:
    media = BlockingGateMedia()
    session = make_gate_session(monkeypatch, media)
    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)
    session.connection_epoch = 1
    session._speaker.start()
    try:

        async def scenario() -> None:
            configured = asyncio.Event()
            configured.set()
            task = asyncio.create_task(session._wake_readiness_loop(threading.Event(), configured, 1))
            assert await asyncio.to_thread(media.write_started.wait, 1.0)
            session.connection_epoch = 2
            media.release_write.set()
            await asyncio.wait_for(task, timeout=1.0)

        asyncio.run(scenario())
    finally:
        media.release_write.set()
        session._speaker.close()

    assert session.input_ready is False
    assert "wake.input_gate_opened" not in [name for name, _fields in recorder.events]


def test_ready_beep_generation_failure_stops_startup(monkeypatch) -> None:
    class BrokenOutputMedia(GateMedia):
        def get_output_audio_samplerate(self) -> int:
            raise RuntimeError("output rate unavailable")

    session = make_gate_session(monkeypatch, BrokenOutputMedia())
    session.connection_epoch = 1
    stop_event = threading.Event()
    session._speaker.start()
    try:

        async def scenario() -> None:
            configured = asyncio.Event()
            configured.set()
            await session._wake_readiness_loop(stop_event, configured, 1)

        asyncio.run(scenario())
    finally:
        session._speaker.close()

    assert stop_event.is_set() is True
    assert session.startup_failure_stage == "ready_beep_generation"
    assert session.input_ready is False


def test_wake_startup_connection_failure_does_not_reconnect(monkeypatch) -> None:
    session = make_gate_session(monkeypatch, GateMedia())

    async def fail_connection(stop_event: object) -> None:
        raise ConnectionError("startup connection lost")

    session._run_connection = fail_connection  # type: ignore[method-assign]
    outcome = asyncio.run(session._run_reconnect_loop(threading.Event()))
    assert outcome is SessionOutcome.STOPPED
    assert session.connection_epoch == 1
    assert session.startup_failure_stage == "connection"
