# ABOUTME: Wake startup data-flow tests for the local ready beep and input gate.
# ABOUTME: Covers discard ordering, stop/epoch races, and pre-ready reconnect suppression.

import asyncio
import base64
import logging
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import FakeRealtimeClient, FakeRecorder, ScriptedConnection, realtime_event

from reachy_openai_realtime import realtime as realtime_mod
from reachy_openai_realtime.audio.capture import CaptureWorker
from reachy_openai_realtime.audio.fanout import AudioFrame, AudioSubscription
from reachy_openai_realtime.audio.playback import SpeakerWorker
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.presence.manager import PresenceManager
from reachy_openai_realtime.presence.states import PresenceState
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState
from reachy_openai_realtime.session.recovery import SessionOutcome
from reachy_openai_realtime.vad import EnergyTurnDetector


class GateMedia:
    def __init__(self) -> None:
        self.pushed: list[np.ndarray] = []

    def get_input_audio_samplerate(self) -> int:
        return 16_000

    def get_output_audio_samplerate(self) -> int:
        return 24_000

    def push_audio_sample(self, data: np.ndarray) -> None:
        self.pushed.append(data)


class QueuedGateMedia(GateMedia):
    def __init__(self) -> None:
        super().__init__()
        self._samples: queue.Queue[np.ndarray] = queue.Queue()

    def feed(self, samples: np.ndarray) -> None:
        self._samples.put(samples)

    def get_audio_sample(self) -> np.ndarray | None:
        try:
            return self._samples.get(timeout=0.05)
        except queue.Empty:
            return None


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

    def stop_current(self, *, reason: str = "stop") -> None:
        pass

    def emotion_names(self) -> list[str]:
        return []

    def dance_names(self) -> list[str]:
        return []


class GateCapture:
    def __init__(self) -> None:
        self.subscription = AudioSubscription("realtime")

    def subscribe(self, name: str) -> AudioSubscription:
        assert name == "realtime"
        return self.subscription

    def unsubscribe(self, name: str) -> None:
        assert name == "realtime"

    def frame_age_seconds(self) -> float:
        return 0.0


class StopAfterPopSubscription:
    dropped_frames = 0

    def __init__(self, stop_event: threading.Event, frame: AudioFrame) -> None:
        self._stop_event = stop_event
        self._frame = frame

    def pop(self, timeout_seconds: float) -> AudioFrame:
        self._stop_event.set()
        return self._frame


class CountingInputBuffer:
    def __init__(self) -> None:
        self.appended = 0
        self.committed = 0
        self.audio: list[str] = []

    async def append(self, *, audio: str) -> None:
        self.appended += 1
        self.audio.append(audio)

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


def test_frame_straddling_gate_is_discarded_before_vad_or_append(monkeypatch) -> None:
    class CountingVad(EnergyTurnDetector):
        def __init__(self) -> None:
            super().__init__()
            self.process_calls = 0

        def process(self, *args, **kwargs):
            self.process_calls += 1
            return super().process(*args, **kwargs)

    async def scenario() -> tuple[RealtimeRobotSession, CountingInputBuffer, CountingVad]:
        session = make_gate_session(monkeypatch, GateMedia())
        subscription = AudioSubscription("realtime")
        session._audio = subscription
        input_buffer = CountingInputBuffer()
        session.connection = SimpleNamespace(
            input_audio_buffer=input_buffer,
            response=CountingResponse(),
        )
        cutoff = time.monotonic()
        samples = np.full(320, 20_000, dtype=np.int16)
        session._input_ready_at = cutoff
        vad = CountingVad()
        vad.begin_turn()
        session._vad = vad
        session.fsm.transition(SessionState.CONNECTING, reason="test")
        session.fsm.transition(SessionState.INITIALIZING, reason="test")
        session.fsm.transition(SessionState.LISTENING, reason="test")
        stop_event = threading.Event()
        task = asyncio.create_task(session._record_loop(stop_event))
        subscription._offer(
            AudioFrame(
                samples=samples,
                sample_rate=16_000,
                captured_at=cutoff + 0.01,
            )
        )
        deadline = time.monotonic() + 1.0
        while (
            session._discarded_wake_frames == 0
            and input_buffer.appended == 0
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)
        return session, input_buffer, vad

    session, input_buffer, vad = asyncio.run(scenario())

    assert session._discarded_wake_frames == 1
    assert input_buffer.appended == 0
    assert vad.process_calls == 0
    assert vad.speech_active is False


def test_frame_returned_after_stop_closes_gate_without_processing(monkeypatch) -> None:
    session = make_gate_session(monkeypatch, GateMedia())
    input_buffer = CountingInputBuffer()
    session.connection = SimpleNamespace(
        input_audio_buffer=input_buffer,
        response=CountingResponse(),
    )
    session._input_ready_at = 0.0
    session.fsm.transition(SessionState.CONNECTING, reason="test")
    session.fsm.transition(SessionState.INITIALIZING, reason="test")
    session.fsm.transition(SessionState.LISTENING, reason="test")
    session._vad.begin_turn()
    stop_event = threading.Event()
    session._audio = StopAfterPopSubscription(
        stop_event,
        AudioFrame(
            samples=np.full(160, 20_000, dtype=np.int16),
            sample_rate=16_000,
            captured_at=time.monotonic(),
        ),
    )

    asyncio.run(session._record_loop(stop_event))

    assert input_buffer.appended == 0
    assert session._vad.speech_active is False
    assert session.input_ready is False


def test_final_session_stop_closes_open_input_gate(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-wake-ready-gate")
    capture = GateCapture()
    session = RealtimeRobotSession(
        robot=SimpleNamespace(media=GateMedia()),
        motion=GateMotion(),
        config=AppConfig(),
        status=RuntimeStatus(),
        capture=capture,
        wake_session=True,
    )
    session._input_ready_at = 0.0
    stop_event = threading.Event()
    stop_event.set()

    outcome = asyncio.run(session.run(stop_event))

    assert outcome is SessionOutcome.STOPPED
    assert session.input_ready is False


def test_real_capture_discards_pre_gate_pcm_and_accepts_post_gate_pcm_in_order(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-wake-ready-gate")
    media = QueuedGateMedia()
    capture = CaptureWorker(media)
    capture.start()
    session = RealtimeRobotSession(
        robot=SimpleNamespace(media=media),
        motion=GateMotion(),
        config=AppConfig(),
        status=RuntimeStatus(),
        capture=capture,
        wake_session=True,
    )
    input_buffer = CountingInputBuffer()
    session.connection = SimpleNamespace(
        input_audio_buffer=input_buffer,
        response=CountingResponse(),
    )
    session._audio = capture.subscribe("realtime")
    session.fsm.transition(SessionState.CONNECTING, reason="test")
    session.fsm.transition(SessionState.INITIALIZING, reason="test")
    session.fsm.transition(SessionState.LISTENING, reason="test")
    stop_event = threading.Event()

    async def scenario() -> None:
        task = asyncio.create_task(session._record_loop(stop_event))
        media.feed(np.full(160, 0.3, dtype=np.float32))
        deadline = time.monotonic() + 1.0
        while session._discarded_wake_frames < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        frame_seconds = 160 / 16_000
        session._input_ready_at = time.monotonic() - (2 * frame_seconds)
        session._vad.begin_turn()
        media.feed(np.full(160, 0.1, dtype=np.float32))
        media.feed(np.full(160, 0.2, dtype=np.float32))
        deadline = time.monotonic() + 1.0
        while input_buffer.appended < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)

    try:
        asyncio.run(scenario())
    finally:
        capture.unsubscribe("realtime")
        capture.close()

    decoded = [np.frombuffer(base64.b64decode(audio), dtype=np.int16) for audio in input_buffer.audio]
    assert session._discarded_wake_frames == 1
    assert input_buffer.appended == 2
    assert len(decoded) == 2
    assert float(np.mean(decoded[0])) < float(np.mean(decoded[1]))
    assert input_buffer.committed == 0
    assert session.connection.response.created == 0


def test_ready_beep_opens_gate_before_ready_callback(monkeypatch) -> None:
    media = GateMedia()
    acceptance_gate_values: list[bool] = []
    callback_gate_values: list[bool] = []
    session = make_gate_session(monkeypatch, media)
    session._accept_session_ready = lambda: (
        acceptance_gate_values.append(session.input_ready) or True
    )
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
    assert acceptance_gate_values == [False]
    assert callback_gate_values == [True]


@pytest.mark.parametrize(
    ("cancel_before_acceptance", "gate_opens"),
    [(True, False), (False, True)],
)
def test_presence_acceptance_linearizes_manual_cancel(
    monkeypatch,
    cancel_before_acceptance: bool,
    gate_opens: bool,
) -> None:
    class PresenceGateMotion(GateMotion):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def wake_acknowledge(self) -> None:
            self.calls.append("wake")

        def connection_failed_motion(self) -> None:
            self.calls.append("fail")

        def sleeping_pose(self) -> None:
            self.calls.append("sleep")

    acceptance_poised = threading.Event()
    release_acceptance = threading.Event()
    callback_gate_values: list[bool] = []
    media = GateMedia()
    motion = PresenceGateMotion()
    status = RuntimeStatus()
    recorder = FakeRecorder()
    status.attach_recorder(recorder)
    session_holder: list[RealtimeRobotSession] = []
    input_buffer = CountingInputBuffer()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-wake-ready-cancel-race")

    def session_factory(
        *,
        wake_session,
        accept_session_ready,
        on_session_ready=None,
    ):
        assert wake_session is True

        def gated_acceptance() -> bool:
            assert accept_session_ready is not None
            if cancel_before_acceptance:
                acceptance_poised.set()
                assert release_acceptance.wait(timeout=1.0)
                return accept_session_ready()
            accepted = accept_session_ready()
            acceptance_poised.set()
            assert release_acceptance.wait(timeout=1.0)
            return accepted

        def counted_ready() -> None:
            session = session_holder[0]
            callback_gate_values.append(session.input_ready)
            assert on_session_ready is not None
            on_session_ready()

        session = RealtimeRobotSession(
            robot=SimpleNamespace(media=media),
            motion=motion,
            config=AppConfig(),
            status=status,
            capture=SimpleNamespace(frame_age_seconds=lambda: 0.0),
            wake_session=True,
            accept_session_ready=gated_acceptance,
            on_session_ready=counted_ready,
        )
        session.connection_epoch = 1
        session.fsm.transition(SessionState.CONNECTING, reason="test")
        session.fsm.transition(SessionState.INITIALIZING, reason="test")
        session._audio = AudioSubscription("realtime")
        session.connection = SimpleNamespace(
            input_audio_buffer=input_buffer,
            response=CountingResponse(),
        )
        session._audio._offer(
            AudioFrame(
                samples=np.full(160, 20_000, dtype=np.int16),
                sample_rate=16_000,
                captured_at=time.monotonic(),
            )
        )
        session._speaker.start()

        async def skip_guard(_stop_event, _seconds: float) -> None:
            return None

        async def run_readiness(stop_event) -> SessionOutcome:
            configured = asyncio.Event()
            configured.set()
            session._sleep_unless_stopped = skip_guard  # type: ignore[method-assign]
            record_task = asyncio.create_task(session._record_loop(stop_event))
            try:
                await session._wake_readiness_loop(stop_event, configured, 1)
            finally:
                stop_event.set()
                await asyncio.wait_for(record_task, timeout=1.0)
            return SessionOutcome.STOPPED

        session.run = run_readiness  # type: ignore[method-assign]
        session_holder.append(session)
        return session

    manager = PresenceManager(
        capture=SimpleNamespace(),
        detector=None,
        motion=motion,
        session_factory=session_factory,
        status=status,
        connect_timeout_seconds=10.0,
    )
    manager._app_stop = threading.Event()
    manager._states.transition(PresenceState.SLEEPING, reason="boot_complete")
    assert manager.request_wake()["ok"] is True
    pending = manager._pending
    assert pending is not None

    session_thread = threading.Thread(target=manager._run_session, args=(pending,), daemon=True)
    session_thread.start()
    sleep_result: dict[str, object] | None = None
    try:
        assert acceptance_poised.wait(timeout=1.0)
        sleep_result = manager.request_sleep()
    finally:
        release_acceptance.set()
        session_thread.join(timeout=1.0)
        if session_holder:
            session_holder[0]._speaker.close()

    assert not session_thread.is_alive()
    assert sleep_result == {"ok": True, "state": "sleeping"}
    session = session_holder[0]
    names = [name for name, _fields in recorder.events]
    assert ("wake.input_gate_opened" in names) is gate_opens
    assert ("wake.ready_beep_completed" in names) is gate_opens
    assert ("wake.session_ready" in names) is gate_opens
    assert callback_gate_values == ([True] if gate_opens else [])
    assert any(
        name == "presence.transition" and fields.get("to_state") == "awake"
        for name, fields in recorder.events
    ) is gate_opens
    assert input_buffer.appended == 0
    assert input_buffer.committed == 0
    assert session.connection.response.created == 0
    assert manager.state is PresenceState.SLEEPING
    assert "fail" not in motion.calls


def test_presence_deadline_stops_real_readiness_loop_before_gate_opens(monkeypatch) -> None:
    class PresenceGateMotion(GateMotion):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def wake_acknowledge(self) -> None:
            self.calls.append("wake")

        def connection_failed_motion(self) -> None:
            self.calls.append("fail")

        def sleeping_pose(self) -> None:
            self.calls.append("sleep")

    now = [100.0]
    callback_count = 0
    media = GateMedia()
    motion = PresenceGateMotion()
    status = RuntimeStatus()
    recorder = FakeRecorder()
    status.attach_recorder(recorder)
    session_holder: list[RealtimeRobotSession] = []

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-wake-ready-deadline")
    monkeypatch.setattr(realtime_mod, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def session_factory(
        *,
        wake_session,
        accept_session_ready,
        on_session_ready=None,
    ):
        nonlocal callback_count
        assert wake_session is True

        def counted_ready() -> None:
            nonlocal callback_count
            callback_count += 1
            assert on_session_ready is not None
            on_session_ready()

        session = RealtimeRobotSession(
            robot=SimpleNamespace(media=media),
            motion=motion,
            config=AppConfig(),
            status=status,
            capture=SimpleNamespace(frame_age_seconds=lambda: 0.0),
            wake_session=True,
            accept_session_ready=accept_session_ready,
            on_session_ready=counted_ready,
        )
        session.connection_epoch = 1
        session.fsm.transition(SessionState.CONNECTING, reason="test")
        session.fsm.transition(SessionState.INITIALIZING, reason="test")
        session._speaker.start()

        async def advance_to_deadline(_stop_event, _seconds: float) -> None:
            now[0] = 100.0 + startup_timeout

        async def run_readiness(stop_event) -> SessionOutcome:
            configured = asyncio.Event()
            configured.set()
            session._sleep_unless_stopped = advance_to_deadline  # type: ignore[method-assign]
            await session._wake_readiness_loop(stop_event, configured, 1)
            return SessionOutcome.STOPPED

        session.run = run_readiness  # type: ignore[method-assign]
        session_holder.append(session)
        return session

    startup_timeout = (
        realtime_mod.READY_BEEP_DURATION_MS / 1_000.0
        + realtime_mod.READY_BEEP_OUTPUT_GUARD_SECONDS
    )
    manager = PresenceManager(
        capture=SimpleNamespace(),
        detector=None,
        motion=motion,
        session_factory=session_factory,
        status=status,
        connect_timeout_seconds=startup_timeout,
        clock=lambda: now[0],
    )
    manager._app_stop = threading.Event()
    manager._states.transition(PresenceState.SLEEPING, reason="boot_complete")
    assert manager.request_wake()["ok"] is True
    pending = manager._pending
    assert pending is not None

    try:
        manager._run_session(pending)
    finally:
        if session_holder:
            session_holder[0]._speaker.close()

    session = session_holder[0]
    names = [name for name, _fields in recorder.events]
    assert session.input_ready is False
    assert "wake.input_gate_opened" not in names
    assert "wake.session_ready" not in names
    assert callback_count == 0
    assert manager.state is PresenceState.SLEEPING
    assert not any(
        name == "presence.transition" and fields.get("to_state") == "awake"
        for name, fields in recorder.events
    )
    assert recorder.events.count(("wake.connection_failed", {"stage": "deadline"})) == 1
    assert motion.calls.count("fail") == 1


def test_ready_beep_guard_starts_after_slow_generation(monkeypatch) -> None:
    media = GateMedia()
    session = make_gate_session(monkeypatch, media)
    session.connection_epoch = 1
    session.fsm.transition(SessionState.CONNECTING, reason="test")
    session.fsm.transition(SessionState.INITIALIZING, reason="test")
    session._speaker.start()
    clock = SimpleNamespace(now=10.0)
    requested_sleeps: list[float] = []
    make_beep = realtime_mod.make_ready_beep

    def slow_make_beep(sample_rate: int) -> np.ndarray:
        clock.now += 5.0
        return make_beep(sample_rate)

    async def record_sleep(stop_event: threading.Event, seconds: float) -> None:
        requested_sleeps.append(seconds)

    monkeypatch.setattr(realtime_mod, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(realtime_mod, "make_ready_beep", slow_make_beep)
    session._sleep_unless_stopped = record_sleep  # type: ignore[method-assign]
    try:

        async def scenario() -> None:
            configured = asyncio.Event()
            configured.set()
            await session._wake_readiness_loop(threading.Event(), configured, 1)

        asyncio.run(scenario())
    finally:
        session._speaker.close()

    expected_guard = (
        realtime_mod.READY_BEEP_DURATION_MS / 1_000.0
        + realtime_mod.READY_BEEP_OUTPUT_GUARD_SECONDS
    )
    assert len(requested_sleeps) == 1
    assert abs(requested_sleeps[0] - expected_guard) < 1e-9


def test_session_updated_keeps_consuming_events_while_ready_beep_is_blocked(monkeypatch) -> None:
    media = BlockingGateMedia()
    session = make_gate_session(monkeypatch, media)
    connection = ScriptedConnection(
        [
            realtime_event("session.updated", session=None),
            realtime_event("rate_limits.updated"),
        ]
    )
    session.client = FakeRealtimeClient([connection])
    session._audio = AudioSubscription("realtime")
    session.connection_epoch = 1
    session.fsm.transition(SessionState.CONNECTING, reason="test")
    stop_event = threading.Event()
    session._speaker.start()
    try:

        async def scenario() -> None:
            task = asyncio.create_task(session._run_connection(stop_event))
            assert await asyncio.to_thread(media.write_started.wait, 1.0)
            deadline = time.monotonic() + 1.0
            while (
                session.status.snapshot()["realtime_event_counts"].get("rate_limits.updated", 0)
                < 1
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.01)

            snapshot = session.status.snapshot()
            assert snapshot["realtime_event_counts"]["session.updated"] == 1
            assert snapshot["realtime_event_counts"]["rate_limits.updated"] == 1
            assert session.fsm.state is SessionState.INITIALIZING
            assert session.input_ready is False
            assert connection.input_audio_buffer.appended == 0
            assert connection.input_audio_buffer.committed == 0
            assert connection.response.created == []

            media.release_write.set()
            deadline = time.monotonic() + 1.0
            while not session.input_ready and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert session.input_ready is True
            assert session.fsm.state is SessionState.LISTENING
            stop_event.set()
            await asyncio.wait_for(task, timeout=1.0)

        asyncio.run(scenario())
    finally:
        media.release_write.set()
        session._speaker.close()

    assert len(media.pushed) == 1
    assert connection.input_audio_buffer.committed == 0
    assert connection.response.created == []


def test_ready_beep_enqueue_failure_stops_startup(monkeypatch) -> None:
    media = GateMedia()
    session = make_gate_session(monkeypatch, media)
    session.connection_epoch = 1
    session._speaker = SpeakerWorker(media, inbox_max=1)
    assert session._speaker.submit(
        np.zeros(1, dtype=np.float32),
        duration_ms=1.0,
        received_at=time.monotonic(),
        timeout_seconds=0.0,
    )
    stop_event = threading.Event()
    try:

        async def scenario() -> None:
            configured = asyncio.Event()
            configured.set()
            await session._wake_readiness_loop(stop_event, configured, 1)

        asyncio.run(scenario())
    finally:
        session._speaker.close()

    assert stop_event.is_set() is True
    assert session.startup_failure_stage == "ready_beep_enqueue"
    assert session.input_ready is False
    assert media.pushed == []


def test_ready_beep_speaker_write_failure_stops_startup(monkeypatch, caplog) -> None:
    class FailingGateMedia(GateMedia):
        def push_audio_sample(self, data: np.ndarray) -> None:
            raise RuntimeError("speaker unavailable")

    media = FailingGateMedia()
    session = make_gate_session(monkeypatch, media)
    session.connection_epoch = 1
    stop_event = threading.Event()
    session._speaker.start()
    try:

        async def scenario() -> None:
            configured = asyncio.Event()
            configured.set()
            await session._wake_readiness_loop(stop_event, configured, 1)

        with caplog.at_level(logging.ERROR):
            asyncio.run(scenario())
    finally:
        session._speaker.close()

    assert stop_event.is_set() is True
    assert session.startup_failure_stage == "ready_beep_write"
    assert session.input_ready is False
    assert "speaker write failed" in caplog.text


def test_awake_wake_session_reconnects_without_closing_gate_or_replaying_beep(monkeypatch) -> None:
    media = GateMedia()
    session = make_gate_session(monkeypatch, media)
    session._input_ready_at = time.monotonic()
    session._audio = AudioSubscription("realtime")
    first = ScriptedConnection(
        [realtime_event("session.updated", session=None)],
        raise_after=ConnectionError("wifi lost"),
    )
    second_ready = threading.Event()
    second = ScriptedConnection(
        [realtime_event("session.updated", session=None)],
        on_drained=second_ready.set,
    )
    session.client = FakeRealtimeClient([first, second])

    async def no_backoff(stop_event: threading.Event, seconds: float) -> None:
        await asyncio.sleep(0)

    session._sleep_unless_stopped = no_backoff  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.create_task(session._run_reconnect_loop(threading.Event()))
        assert await asyncio.to_thread(second_ready.wait, 1.0)
        assert session.connection_epoch == 2
        assert session.input_ready is True
        assert media.pushed == []
        assert first.response.created == []
        assert second.response.created == []
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert session.input_ready is True
    assert session.connection_epoch == 2
    assert session.status.metrics.snapshot()["counters"]["reconnect_count"] == 1


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
