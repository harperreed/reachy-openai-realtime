<!-- ABOUTME: TDD implementation plan for the wake-session input gate and local ready beep. -->
<!-- ABOUTME: Splits speaker acknowledgement, session gating, and presence wiring into reviewable commits. -->

# Wake Ready Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make wake-enabled Reachy discard cold-start audio, play one local ready beep, and accept microphone input only after the beep guard ends.

**Architecture:** Extend the existing `SpeakerWorker` queue with an optional write receipt and generate the ready tone as float32 samples. An explicit `wake_session` flag leaves the session's input-ready timestamp unset until a connection-scoped readiness task receives `session.updated`, writes the beep, waits through its output guard, resets VAD, and records the gate-open time. The record loop rejects both live and queued frames whose `captured_at` precedes that cutoff. `PresenceManager` marks both wake-word and manual sessions explicitly and transitions to `AWAKE` only through the post-beep callback.

**Tech Stack:** Python 3.10+, asyncio, threading, NumPy, pytest, OpenAI Realtime SDK, Reachy Mini media API.

**Estimated change:** 180–260 lines across source and tests.

## Global Constraints

- Keep exactly one microphone owner: `CaptureWorker` continues draining at all times.
- Wake sessions discard every microphone frame captured before the gate opens.
- A pre-gate frame still in the bounded subscription after the gate opens remains rejected by its `captured_at` timestamp.
- Generate one 880 Hz, 160 ms, amplitude-0.15 mono float32 tone at the robot output rate.
- Wait for speaker write acknowledgement, the 160 ms tone duration, and a 100 ms output guard.
- The existing 10-second wake deadline bounds connection, beep write, and output guard.
- Do not call `play_sound`, `stop_playing`, or create a second speaker or microphone pipeline.
- Wake sessions send no greeting, pre-roll, input commit, or response request before the gate opens.
- Wake-disabled mode keeps its current always-connected greeting and input behavior.
- Initial wake startup does not reconnect after a pre-gate connection loss; awake reconnects stay unchanged.
- Keep wake pre-roll storage and public configuration fields, but do not pass pre-roll into Realtime.
- Log only safe event names, counts, durations, states, and connection epochs; never audio or transcripts.
- Preserve the five-turn/60-second breaker, 30-minute ceiling, wake latch, and stop contract.
- Use TDD for each task and commit only after its targeted tests pass.

## File map

- `reachy_openai_realtime/audio/playback.py`: ready-tone generation and optional speaker write receipts.
- `reachy_openai_realtime/realtime.py`: explicit wake-session mode, discard gate, readiness task, greeting rule, and startup-failure stage.
- `reachy_openai_realtime/presence/manager.py`: explicit wake-session factory contract and post-beep presence transition.
- `reachy_openai_realtime/main.py`: wire wake sessions with `wake_session=True`; leave the always-connected constructor unchanged.
- `reachy_openai_realtime/runtime_status.py`: map `WAKING` to the existing connecting phase without claiming an active connection.
- `tests/test_audio_playback.py`: tone and tracked-write unit tests using an in-memory sink.
- `tests/test_wake_ready_gate.py`: session-level gate, beep ordering, stale epoch, and startup reconnect integration tests.
- `tests/test_presence_manager.py`: wake-word/manual factory wiring and failure-stage integration tests.
- `tests/test_runtime_status.py`: WAKING status projection.
- `tests/test_realtime_manual_turn.py`: replace pre-roll greeting/injection expectations with the explicit wake-session contract.
- `tests/test_realtime_fsm.py`: mark hand-built always-connected record-loop sessions as input-ready.

---

### Task 1: Add tracked speaker writes and the local ready tone

**Files:**
- Modify: `reachy_openai_realtime/audio/playback.py`
- Test: `tests/test_audio_playback.py`

**Interfaces:**
- Produces: `make_ready_beep(sample_rate: int) -> np.ndarray`.
- Produces: `SpeakerWriteReceipt.done() -> bool` and `SpeakerWriteReceipt.succeeded() -> bool`.
- Produces: `SpeakerWorker.submit_tracked(pcm, duration_ms, received_at, timeout_seconds) -> SpeakerWriteReceipt | None`.
- Preserves: `SpeakerWorker.submit(pcm: np.ndarray, duration_ms: float, received_at: float, timeout_seconds: float) -> bool` for normal response playback.

- [x] **Step 1: Write failing tone and receipt tests**

Add imports and these tests to `tests/test_audio_playback.py`:

```python
import logging

from reachy_openai_realtime.audio.playback import (
    READY_BEEP_AMPLITUDE,
    READY_BEEP_DURATION_MS,
    READY_BEEP_FREQUENCY_HZ,
    PlaybackBuffer,
    PlaybackChunk,
    SpeakerWorker,
    make_ready_beep,
)


def test_ready_beep_has_expected_shape_frequency_and_amplitude() -> None:
    for sample_rate in (16_000, 48_000):
        beep = make_ready_beep(sample_rate)
        assert beep.dtype == np.float32
        assert beep.ndim == 1
        assert len(beep) == round(sample_rate * READY_BEEP_DURATION_MS / 1_000.0)
        assert float(np.max(np.abs(beep))) <= READY_BEEP_AMPLITUDE + np.finfo(np.float32).eps
        frequencies = np.fft.rfftfreq(len(beep), d=1.0 / sample_rate)
        peak = frequencies[int(np.argmax(np.abs(np.fft.rfft(beep))))]
        assert abs(peak - READY_BEEP_FREQUENCY_HZ) <= sample_rate / len(beep)


def test_tracked_speaker_write_reports_success() -> None:
    media = FakeSpeakerMedia()
    worker = SpeakerWorker(media)
    worker.start()
    try:
        beep = make_ready_beep(24_000)
        receipt = worker.submit_tracked(
            beep,
            READY_BEEP_DURATION_MS,
            time.monotonic(),
            timeout_seconds=1.0,
        )
        assert receipt is not None
        deadline = time.monotonic() + 2.0
        while not receipt.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert receipt.done() is True
        assert receipt.succeeded() is True
        assert len(media.pushed) == 1
        np.testing.assert_array_equal(media.pushed[0], beep)
    finally:
        worker.close()


def test_tracked_speaker_write_reports_failure(caplog) -> None:
    class FailingMedia:
        def push_audio_sample(self, data: np.ndarray) -> None:
            raise RuntimeError("speaker unavailable")

    with caplog.at_level(logging.ERROR):
        worker = SpeakerWorker(FailingMedia())
        worker.start()
        try:
            receipt = worker.submit_tracked(
                make_ready_beep(24_000),
                READY_BEEP_DURATION_MS,
                time.monotonic(),
                timeout_seconds=1.0,
            )
            assert receipt is not None
            deadline = time.monotonic() + 2.0
            while not receipt.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert receipt.done() is True
            assert receipt.succeeded() is False
        finally:
            worker.close()
    assert "speaker write failed" in caplog.text


def test_flush_fails_a_queued_tracked_write() -> None:
    worker = SpeakerWorker(FakeSpeakerMedia())
    receipt = worker.submit_tracked(
        make_ready_beep(24_000),
        READY_BEEP_DURATION_MS,
        time.monotonic(),
        timeout_seconds=0.1,
    )
    assert receipt is not None
    worker.flush()
    assert receipt.done() is True
    assert receipt.succeeded() is False
```

- [x] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/test_audio_playback.py -k 'ready_beep or tracked_speaker or queued_tracked' -v
```

Expected: collection fails because the new constants, function, and receipt API do not exist.

- [x] **Step 3: Implement the tone and one shared queue-item path**

Add these definitions near the playback constants in `reachy_openai_realtime/audio/playback.py`:

```python
READY_BEEP_FREQUENCY_HZ = 880.0
READY_BEEP_DURATION_MS = 160.0
READY_BEEP_AMPLITUDE = 0.15


def make_ready_beep(sample_rate: int) -> np.ndarray:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    sample_count = round(sample_rate * READY_BEEP_DURATION_MS / 1_000.0)
    positions = np.arange(sample_count, dtype=np.float32) / np.float32(sample_rate)
    return (READY_BEEP_AMPLITUDE * np.sin(2.0 * np.pi * READY_BEEP_FREQUENCY_HZ * positions)).astype(
        np.float32
    )


class SpeakerWriteReceipt:
    def __init__(self) -> None:
        self._done = threading.Event()
        self._succeeded = False

    def resolve(self, *, succeeded: bool) -> None:
        self._succeeded = succeeded
        self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def succeeded(self) -> bool:
        return self._done.is_set() and self._succeeded


@dataclass
class _SpeakerWrite:
    pcm: np.ndarray
    duration_ms: float
    received_at: float
    receipt: SpeakerWriteReceipt | None = None
```

Change the speaker inbox to `queue.Queue[_SpeakerWrite]`. Keep `submit()` as the normal untracked path, add `submit_tracked()`, and share this helper:

```python
def _submit(self, write: _SpeakerWrite, timeout_seconds: float) -> bool:
    try:
        self._inbox.put(write, timeout=timeout_seconds)
        return True
    except queue.Full:
        return False

def submit(self, pcm: np.ndarray, duration_ms: float, received_at: float, timeout_seconds: float) -> bool:
    return self._submit(_SpeakerWrite(pcm, duration_ms, received_at), timeout_seconds)

def submit_tracked(
    self,
    pcm: np.ndarray,
    duration_ms: float,
    received_at: float,
    timeout_seconds: float,
) -> SpeakerWriteReceipt | None:
    receipt = SpeakerWriteReceipt()
    write = _SpeakerWrite(pcm, duration_ms, received_at, receipt)
    return receipt if self._submit(write, timeout_seconds) else None
```

Make `flush()` resolve queued receipts as failed. In `_run()`, resolve a tracked item as failed on a write exception and successful immediately after `push_audio_sample()` returns. Preserve the existing `on_write`, `last_write_at`, and `frames_total` behavior for every successful item:

```python
def flush(self) -> None:
    while True:
        try:
            write = self._inbox.get_nowait()
        except queue.Empty:
            return
        if write.receipt is not None:
            write.receipt.resolve(succeeded=False)

def _run(self) -> None:
    while not self._stop.is_set():
        try:
            write = self._inbox.get(timeout=0.25)
        except queue.Empty:
            continue
        try:
            self._media.push_audio_sample(write.pcm)
        except Exception:
            logger.exception("speaker write failed")
            if write.receipt is not None:
                write.receipt.resolve(succeeded=False)
            continue
        self.last_write_at = time.monotonic()
        self.frames_total += 1
        if write.receipt is not None:
            write.receipt.resolve(succeeded=True)
        if self._on_write is not None:
            try:
                self._on_write(write.duration_ms, write.received_at)
            except Exception:
                logger.exception("on_write callback failed")
```

- [x] **Step 4: Run the playback tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_audio_playback.py -v
```

Expected: all playback tests pass with the four new cases.

- [x] **Step 5: Commit Task 1**

```bash
git status --short
git add reachy_openai_realtime/audio/playback.py tests/test_audio_playback.py
git commit -m "feat: add tracked ready beep playback"
```

---

### Task 2: Gate wake-session input until the ready beep

**Files:**
- Modify: `reachy_openai_realtime/realtime.py`
- Create: `tests/test_wake_ready_gate.py`
- Modify: `tests/test_realtime_manual_turn.py`
- Modify: `tests/test_realtime_fsm.py`

**Interfaces:**
- Consumes: `make_ready_beep`, `READY_BEEP_DURATION_MS`, and `SpeakerWorker.submit_tracked()` from Task 1.
- Produces: constructor parameters `wake_session: bool = False` and `on_session_ready: Callable[[], None] | None = None` on `RealtimeRobotSession`.
- Produces: read-only `RealtimeRobotSession.startup_failure_stage: str | None` for presence-level failure logging.
- Preserves: an already-awake socket reconnect does not replay the beep or close the session-wide input gate.

- [x] **Step 1: Write failing session contract tests**

Create `tests/test_wake_ready_gate.py` with this complete starting content. It uses a real `SpeakerWorker`, `RuntimeStatus`, `SessionStateMachine`, `EnergyTurnDetector`, and `AudioSubscription`; the in-memory media sink captures PCM data without a mock framework:

```python
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
```

Replace the greeting tests in `tests/test_realtime_manual_turn.py` with the explicit contract:

```python
def test_should_send_greeting_false_for_wake_session() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session._greeting_sent = False
    session._wake_session = True
    assert session._should_send_greeting() is False


def test_should_send_greeting_true_for_initial_always_connected_session() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session._greeting_sent = False
    session._wake_session = False
    assert session._should_send_greeting() is True


def test_should_send_greeting_false_after_always_connected_greeting() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session._greeting_sent = True
    session._wake_session = False
    assert session._should_send_greeting() is False
```

Delete the old test that expects pending wake audio to be injected as an opening turn. Its required replacement is the closed-gate data-flow test above; this changes expected behavior without reducing coverage.

In the hand-built record-loop session helpers in `tests/test_realtime_manual_turn.py` and `tests/test_realtime_fsm.py`, replace `_pending_wake_audio` and `_wake_ready` setup with:

```python
session._wake_session = False
session._input_ready_at = 0.0
```

Remove the now-unused `AudioFrame` test import.

- [x] **Step 2: Run the session tests and verify RED**

Run:

```bash
uv run pytest tests/test_wake_ready_gate.py tests/test_realtime_manual_turn.py -k 'wake or greeting' -v
```

Expected: failures show that `wake_session`, `_wake_readiness_loop`, and the input gate do not exist, and the old greeting rule still depends on buffered audio.

- [x] **Step 3: Add explicit wake-session state and remove Realtime pre-roll consumption**

In `RealtimeRobotSession.__init__`, replace `pending_wake_audio` with `wake_session: bool = False` and initialize:

```python
self._wake_session = wake_session
self._input_ready_at: float | None = None if wake_session else 0.0
self._discarded_wake_frames = 0
self._startup_failure_stage: str | None = None
```

Expose readiness and the failure stage without setters:

```python
@property
def input_ready(self) -> bool:
    return self._input_ready_at is not None

@property
def startup_failure_stage(self) -> str | None:
    return self._startup_failure_stage
```

Subscribe with the normal bounded capture subscription in `run()`:

```python
self._audio = self._capture.subscribe("realtime")
```

Remove `_pending_wake_audio`, `_wake_ready`, `_wake_realtime_buffer_ms`, the now-unused `AudioFrame` import, and the wake-turn injection block from `_record_loop`. Immediately after a non-`None` frame is popped, add the only input gate:

```python
ready_at = self._input_ready_at
if ready_at is None or frame.captured_at < ready_at:
    self._discarded_wake_frames += 1
    continue
```

Change greeting selection to:

```python
def _should_send_greeting(self) -> bool:
    return not self._greeting_sent and not self._wake_session
```

Add `_close_input_gate()` to clear the cutoff and reset VAD. Call it from `run()`'s final cleanup,
and recheck `stop_event` immediately after the record loop's blocking `pop()` so a post-stop frame
cannot reach VAD or OpenAI. Do not call it from `reset_connection_state()` because an already-awake
socket reconnect must keep the session-wide cutoff open.

- [x] **Step 4: Implement connection-scoped readiness without blocking socket consumption**

Import the Task 1 beep symbols and add:

```python
READY_BEEP_OUTPUT_GUARD_SECONDS = 0.1

async def _wake_readiness_loop(
    self,
    stop_event: Any,
    session_configured: asyncio.Event,
    epoch: int,
) -> None:
    await session_configured.wait()
    if stop_event.is_set() or epoch != self.connection_epoch:
        return

    started_at = time.monotonic()
    self.status.record_event("wake.ready_beep_started")
    try:
        sample_rate = self.robot.media.get_output_audio_samplerate()
        beep = make_ready_beep(sample_rate)
    except Exception:
        self._startup_failure_stage = "ready_beep_generation"
        stop_event.set()
        return
    submission_started_at = time.monotonic()
    receipt = await asyncio.to_thread(
        self._speaker.submit_tracked,
        beep,
        READY_BEEP_DURATION_MS,
        submission_started_at,
        1.0,
    )
    if receipt is None:
        self._startup_failure_stage = "ready_beep_enqueue"
        stop_event.set()
        return
    submitted_at = time.monotonic()

    while not receipt.done():
        if stop_event.is_set() or epoch != self.connection_epoch:
            return
        await asyncio.sleep(0.01)
    if not receipt.succeeded():
        self._startup_failure_stage = "ready_beep_write"
        stop_event.set()
        return

    ready_at = submitted_at + (READY_BEEP_DURATION_MS / 1_000.0) + READY_BEEP_OUTPUT_GUARD_SECONDS
    await self._sleep_unless_stopped(stop_event, max(0.0, ready_at - time.monotonic()))
    if stop_event.is_set() or epoch != self.connection_epoch:
        return

    self._vad.reset_turn()
    gate_opened_at = time.monotonic()
    self._input_ready_at = gate_opened_at
    dropped_by_subscription = self._audio.dropped_frames if self._audio is not None else 0
    session_started_at = self._session_started_at or started_at
    self.status.record_event(
        "wake.ready_beep_completed",
        startup_duration_seconds=round(gate_opened_at - session_started_at, 3),
        discarded_frames=self._discarded_wake_frames + dropped_by_subscription,
    )
    self.status.record_event("wake.input_gate_opened", epoch=epoch)
    self._enter_listening(reason="wake_ready")
```

Extract the existing LISTENING transition, connected listening phase, and ready callback into a synchronous `_enter_listening()` helper. The helper must set the session FSM and listening phase before invoking `_on_session_ready`; the callback therefore cannot publish `AWAKE` while the session still rejects input.

Use this exact helper:

```python
def _enter_listening(self, *, reason: str) -> None:
    self.fsm.transition(SessionState.LISTENING, reason=reason)
    self.status.set_phase(
        "listening",
        self._listening_detail(connected=True) + "（ロボット側で無音800msを判定）",
        connected=True,
        event=True,
        detail_key="detail_listening_connected",
        detail_params=self._listening_params(),
    )
    if self._on_session_ready is not None:
        self._on_session_ready()
```

In `_run_connection`, create one event per connection epoch and use this task construction:

```python
session_configured = asyncio.Event()
tasks = [
    asyncio.create_task(self._record_loop(stop_event), name="record-loop"),
    asyncio.create_task(self._playback_loop(stop_event), name="playback-loop"),
    asyncio.create_task(
        self._event_loop(stop_event, session_configured),
        name="event-loop",
    ),
    asyncio.create_task(self._watchdog_loop(), name="watchdog-loop"),
    asyncio.create_task(self._supervisor_loop(stop_event), name="supervisor-loop"),
]
if self._wake_session and not self.input_ready:
    tasks.append(
        asyncio.create_task(
            self._wake_readiness_loop(
                stop_event,
                session_configured,
                self.connection_epoch,
            ),
            name="wake-readiness-loop",
        )
    )
```

Change the event-loop signature to accept `session_configured: asyncio.Event | None = None`. At method entry, normalize direct test calls with:

```python
if session_configured is None:
    session_configured = asyncio.Event()
```

Replace the current `session.updated` body with `await self._handle_session_updated(session_configured)` and add this complete helper:

```python
async def _handle_session_updated(
    self,
    session_configured: asyncio.Event,
) -> None:
    self.watchdog.disarm("session_update")
    self.status.clear_error()
    self._connected_epoch = self.connection_epoch
    self.status.record_event("realtime.connected", epoch=self.connection_epoch)
    if self._wake_session and not self.input_ready:
        session_configured.set()
        return
    self._enter_listening(reason="session_updated")
    if self._should_send_greeting():
        self._greeting_sent = True
        self.fsm.transition(SessionState.WAITING_RESPONSE, reason="greeting_requested")
        self._response_generation_done = False
        self.watchdog.arm("response_create")
        self.status.record_event("response.requested", reason="greeting")
        await self.connection.response.create(
            response={
                "instructions": greeting_instructions(self._current_language()),
                "output_modalities": ["audio"],
                "tool_choice": "none",
            }
        )
        self.status.record_response_request()
```

After a connection attempt ends in `_run_reconnect_loop`, add this branch before normal recovery/backoff:

```python
if self._wake_session and not self.input_ready:
    if self._startup_failure_stage is None:
        self._startup_failure_stage = "connection"
    self.fsm.transition(SessionState.STOPPING, reason="wake_startup_failed")
    self.fsm.transition(SessionState.DISCONNECTED, reason="shutdown_complete")
    return SessionOutcome.STOPPED
```

Do not reset `_input_ready_at` in `reset_connection_state()`: once the wake session reaches `AWAKE`, that session-wide capture cutoff remains valid across the existing socket reconnect path.

- [x] **Step 5: Run focused session tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_wake_ready_gate.py tests/test_realtime_manual_turn.py tests/test_realtime_reconnect.py tests/test_realtime_reset.py -v
```

Expected: all selected tests pass; wake startup stops after one failed epoch, while existing awake reconnect tests still pass.

- [x] **Step 6: Commit Task 2**

```bash
git status --short
git add reachy_openai_realtime/realtime.py tests/test_wake_ready_gate.py tests/test_realtime_manual_turn.py tests/test_realtime_fsm.py
git commit -m "fix: gate wake input until ready beep"
```

---

### Task 3: Wire wake-word and manual presence through the same gate

**Files:**
- Modify: `reachy_openai_realtime/presence/manager.py`
- Modify: `reachy_openai_realtime/main.py`
- Modify: `reachy_openai_realtime/runtime_status.py`
- Test: `tests/test_presence_manager.py`
- Test: `tests/test_runtime_status.py`

**Interfaces:**
- Consumes: `RealtimeRobotSession` with `wake_session=True`, a concrete ready callback, and `startup_failure_stage` from Task 2.
- Produces: presence session factories accept `wake_session: bool` and `on_session_ready` only; they never receive pre-roll.
- Preserves: `_PendingWake` and `WakeAudioAssembler` remain memory-only so current wake settings stay valid.

- [x] **Step 1: Write failing presence and status tests**

Replace the session test doubles in `tests/test_presence_manager.py` with the explicit factory contract:

```python
class FakeSession:
    startup_failure_stage: str | None = None

    def __init__(self, *, wake_session=False, on_session_ready=None, outcome=SessionOutcome.STOPPED):
        self.wake_session = wake_session
        self._on_session_ready = on_session_ready
        self.outcome = outcome
        self.ran = False

    async def run(self, stop_event):
        self.ran = True
        if self._on_session_ready is not None:
            self._on_session_ready()
        if self.outcome is SessionOutcome.NOISE_BAIL:
            return self.outcome
        while not stop_event.is_set():
            await asyncio.sleep(0.01)
        return self.outcome


class SessionRecorder:
    def __init__(self, *, connect=True, outcomes: list[SessionOutcome] | None = None):
        self.sessions = []
        self._connect = connect
        self._outcomes = list(outcomes or [])

    def __call__(self, *, wake_session=False, on_session_ready=None):
        outcome = self._outcomes.pop(0) if self._outcomes else SessionOutcome.STOPPED
        session = FakeSession(
            wake_session=wake_session,
            on_session_ready=on_session_ready if self._connect else None,
            outcome=outcome,
        )
        self.sessions.append(session)
        return session


class GatedNoiseBailSession:
    startup_failure_stage: str | None = None

    def __init__(self, *, wake_session=False, on_session_ready=None, return_gate=None):
        self.wake_session = wake_session
        self._on_session_ready = on_session_ready
        self._return_gate = return_gate

    async def run(self, stop_event):
        if self._on_session_ready is not None:
            self._on_session_ready()
        if self._return_gate is not None:
            await asyncio.to_thread(self._return_gate.wait)
        return SessionOutcome.NOISE_BAIL
```

Update the one local `session_factory` in the stale-rearm test to accept `wake_session` and pass it to `GatedNoiseBailSession`. Replace the old wake-word test with:

```python
def test_wake_word_builds_ready_gated_session_without_preroll() -> None:
    capture = FakeCapture()
    motion = FakeMotion()
    factory = SessionRecorder(connect=True)
    status = FakeStatus()
    manager = PresenceManager(
        capture=capture,
        detector=FakeDetector(fire_after=3),
        motion=motion,
        session_factory=factory,
        status=status,
        pre_roll_seconds=1.0,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        for _ in range(5):
            capture.feed(_frame())
            time.sleep(0.02)
        assert _wait_until(lambda: manager.state is PresenceState.AWAKE)

    assert "wake" in motion.calls
    assert len(factory.sessions) == 1
    assert factory.sessions[0].wake_session is True
    assert not hasattr(factory.sessions[0], "pending_wake_audio")
    kinds = [event for event, _fields in status.events]
    assert "wake.detected" in kinds
    assert "wake.session_ready" in kinds
```

Extend the manual-wake test with:

```python
assert factory.sessions[0].wake_session is True
assert not hasattr(factory.sessions[0], "pending_wake_audio")
```

Add this startup-failure test:

```python
def test_ready_beep_failure_stage_is_recorded() -> None:
    class FailedReadyBeepSession:
        startup_failure_stage = "ready_beep_write"

        async def run(self, stop_event):
            return SessionOutcome.STOPPED

    status = FakeStatus()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=lambda **_kwargs: FailedReadyBeepSession(),
        status=status,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(
            lambda: (
                "wake.connection_failed",
                {"stage": "ready_beep_write"},
            ) in status.events
        )

```

Add a cancellation test that holds the session in `WAKING` without firing the ready callback:

```python
def test_manual_sleep_cancels_session_while_waking() -> None:
    started = threading.Event()

    class WakingSession:
        startup_failure_stage = None

        async def run(self, stop_event):
            started.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.01)
            return SessionOutcome.STOPPED

    status = FakeStatus()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=lambda **_kwargs: WakingSession(),
        status=status,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert started.wait(timeout=1.0)
        assert manager.state is PresenceState.WAKING
        assert manager.request_sleep()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
    assert not any(event == "wake.connection_failed" for event, _fields in status.events)
```

Replace `test_set_presence_updates_snapshot` in `tests/test_runtime_status.py` with:

```python
def test_waking_presence_reports_connecting_without_claiming_connection() -> None:
    from reachy_openai_realtime.presence.states import PresenceState

    status = RuntimeStatus()
    status.set_presence(PresenceState.SLEEPING, PresenceState.WAKING, "wake_word")
    snapshot = status.snapshot()
    assert snapshot["presence"] == "waking"
    assert snapshot["phase"] == "connecting"
    assert snapshot["connected"] is False
```

- [x] **Step 2: Run presence/status tests and verify RED**

Run:

```bash
uv run pytest tests/test_presence_manager.py tests/test_runtime_status.py -v
```

Expected: session factories reject `wake_session`, the manager still passes pre-roll, failure events lack `stage`, and WAKING does not set the connecting phase.

- [x] **Step 3: Change the presence session contract**

Add `stop_event: threading.Event` and `cancel_reason: str | None = None` to `_PendingWake`. Whenever `_on_wake()` or either manual-wake branch creates a pending wake, create the stop event at the same time and publish it under the lifecycle lock:

```python
pending = _PendingWake(
    wake_audio=wake_audio,
    event=event,
    stop_event=threading.Event(),
)
self._pending = pending
self._session_stop = pending.stop_event
```

For manual wake, use the same code with `wake_audio=None` and `event=None`. In `PresenceManager._run_session`, use `pending.stop_event` rather than creating a new event, then build the session with no audio parameter:

```python
session_stop = pending.stop_event
session = self._session_factory(
    wake_session=True,
    on_session_ready=_on_session_ready,
)
```

At the top of `_on_session_ready`, set `ready` and then return without transitioning if `session_stop.is_set()`. This prevents a deadline or manual-cancel race from publishing `AWAKE`:

```python
ready.set()
if session_stop.is_set():
    return
```

Allow manual sleep in either live startup or awake state. Read and change the pending cancellation reason under the existing lifecycle lock, then set the stop event outside it:

```python
with self._lock:
    state = self._states.state
    if state not in (PresenceState.WAKING, PresenceState.AWAKE):
        return {"ok": False, "state": state.name.lower(), "reason": "not_awake"}
    if state is PresenceState.WAKING and self._pending is not None:
        self._pending.cancel_reason = "manual_sleep"
    session_stop = self._session_stop
if session_stop is not None:
    session_stop.set()
```

Read `pending.cancel_reason` in `_run_session` before clearing `_pending`, and pass it into `_finish_session`. A manual startup cancel transitions back to sleep without a false connection-failure event or failure motion.

Track whether the 10-second watchdog expired with a `threading.Event`. Before `_finish_session`, choose the failure stage from the deadline or the session:

```python
deadline_expired = threading.Event()

def _watch_deadline() -> None:
    if not ready.wait(self._connect_timeout_seconds):
        deadline_expired.set()
        session_stop.set()

failure_stage = (
    "deadline"
    if deadline_expired.is_set()
    else getattr(session, "startup_failure_stage", None)
)
self._finish_session(
    app_stopping=self._app_stop.is_set(),
    failure_stage=failure_stage,
    cancel_reason=pending.cancel_reason,
)
```

Change `_finish_session` to accept `failure_stage: str | None` and `cancel_reason: str | None`. For a non-cancelled startup failure, record exactly one existing failure event with a safe stage:

```python
if state is PresenceState.WAKING and not app_stopping and cancel_reason != "manual_sleep":
    self._status.record_event("wake.connection_failed", stage=failure_stage or "startup")
    if self._wake_motion_enabled:
        self._motion.connection_failed_motion()
```

Update the manual-wake docstring: it starts a ready-gated session with no pre-roll and no model greeting.

Use a deadline-aware combined stop signal for wake startup so the session's final pre-gate
`is_set()` check makes the 10-second deadline authoritative before input opens. Serialize callback,
deadline, manual cancel, app stop, session completion effects, the SLEEPING observer, latch
publication, and lifecycle ownership so one startup outcome wins and a new wake cannot overtake
the old attempt's failure or sleeping motion.

- [x] **Step 4: Wire production and connecting status**

Change the wake-enabled factory in `main.py` to:

```python
def session_factory(*, wake_session, on_session_ready=None):
    return RealtimeRobotSession(
        reachy_mini,
        motion,
        AppConfig.from_env(),
        self.runtime_status,
        capture,
        language_provider=self._current_language,
        camera_enabled=self._is_camera_enabled,
        capture_camera_jpeg=self._capture_camera_frame,
        memory=memory_manager,
        nap=nap,
        wake_session=wake_session,
        on_session_ready=on_session_ready,
    )
```

Leave the wake-disabled `RealtimeRobotSession` constructor call unchanged so its default `wake_session=False` preserves the greeting and immediate input path.

In `RuntimeStatus.set_presence`, map `waking` to the existing connecting copy while holding the existing lock:

```python
if state_name == "waking":
    self._phase = "connecting"
    self._detail = "Realtime APIへ接続しています"
    self._detail_key = "detail_connecting"
    self._detail_params = {}
    self._connected = False
elif state_name == "sleeping":
    self._phase = "sleeping"
    self._detail = "Asleep · say “hey reachy”"
    self._detail_key = "presence_sleeping"
    self._detail_params = {}
    self._connected = False
```

Add `sleeping` to the dashboard's known phases and provide `phase_sleeping` in every supported
locale so the sleeping backend phase does not render as `Starting`.

- [x] **Step 5: Run presence and cross-path tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_presence_manager.py tests/test_runtime_status.py tests/test_app_loop.py tests/test_realtime_config.py -v
```

Expected: all selected tests pass; both wake sources use `wake_session=True`, wake-disabled behavior remains green, and startup failures carry a stage.

- [x] **Step 6: Commit Task 3**

```bash
git status --short
git add reachy_openai_realtime/presence/manager.py reachy_openai_realtime/main.py reachy_openai_realtime/runtime_status.py tests/test_presence_manager.py tests/test_runtime_status.py
git commit -m "fix: route presence through wake ready gate"
```

---

### Task 4: Verify the complete change and prepare hardware acceptance

**Files:**
- Modify only if verification exposes a task-scoped defect.
- Review: `docs/superpowers/specs/2026-08-24-wake-ready-gate-design.md`
- Review: `gotchas.md`

**Interfaces:**
- Consumes: all Task 1–3 behavior.
- Produces: a review-clean branch whose local evidence is ready for night-robot deployment.

- [x] **Step 1: Run the full wake and audio integration slice**

```bash
uv run pytest tests/test_audio_playback.py tests/test_wake_ready_gate.py tests/test_presence_manager.py tests/test_realtime_manual_turn.py tests/test_realtime_reconnect.py tests/test_realtime_reset.py tests/test_app_loop.py tests/test_runtime_status.py -v
```

Expected: all selected tests pass with no warnings or logged errors outside tests that capture and assert the expected speaker-write failure.

- [x] **Step 2: Run the canonical repository gate**

```bash
uv run ruff check . && uv run pytest
```

Expected: Ruff passes and at least the 429 baseline tests plus the new tests pass with no new warnings.

- [x] **Step 3: Run two-stage subagent review and fresh-eyes review**

First review exact spec compliance against `2026-08-24-wake-ready-gate-design.md`. Then review code quality, races, stop behavior, privacy, and whether tests assert state/data flow rather than mock call lists. Fix every in-scope finding with a failing test first, rerun the targeted suite, and commit each reviewed fix with a conventional imperative message.

Final code-review checkpoint: exact code SHA `3a4054b` passed the 145-test expanded integration
slice, Ruff, and 471 repository tests with no warnings. It moves the gate-opening action inside
the lifecycle-lock readiness decision, skips deadline-lock work after startup resolves, and starts
the full 160 ms tone plus 100 ms output guard after speaker-write acknowledgement. Independent spec
and quality reviewers approved that SHA with no remaining findings. Physical night-robot acceptance
is still pending.

- [x] **Step 4: Push a branch and open a pull request**

```bash
git push -u origin fix/wake-ready-gate
gh pr create --draft --title "fix: gate wake input until ready beep" \
  --body-file /tmp/reachy-wake-ready-gate-pr.md
```

Prepare the PR body in a temporary file outside the repository. Include the incident, root cause, behavior change, local test counts, privacy boundary, known physical-playback estimate, and Kata issue `9n71`. Convert from draft only after review findings are resolved and the canonical gate is rerun.

Opened [PR #27](https://github.com/harperreed/reachy-openai-realtime/pull/27) after both
reviewers approved the final code and the canonical gate passed.

- [ ] **Step 5: Run night-robot end-to-end acceptance after deploy approval**

Resolve the target aloud as **night robot `192.168.200.128`** before any command. Confirm the app is stopped, deploy the reviewed PR revision, and run these physical cases with structured counters only:

1. wake phrase only: one beep, zero pre-beep commits, zero pre-beep responses;
2. early speech: utterance before the beep is discarded;
3. post-beep phrase: exactly one commit and one response, with Doctor Biz judging understanding;
4. manual sleep/wake: same one-beep gate; and
5. final state: app stopped and Realtime disconnected.

Never log or retrieve raw audio or transcripts. Do not tuck, reboot, shut down, or call `goto_sleep` without new approval.

- [ ] **Step 6: Record evidence in Kata without closing early**

Before hardware acceptance, keep `9n71` open and comment with commit SHA, PR URL, canonical test count, and review results. After all physical criteria pass and the app is confirmed stopped:

```bash
accepted_sha="$(git rev-parse HEAD)"
kata close 9n71 --done \
  --message "Ready-gated wake discards cold-start audio, beeps once, and passed local plus night-robot acceptance." \
  --commit "$accepted_sha"
```

If any physical criterion fails, leave the issue open, keep or add `needs-review`, and comment with safe event names, counters, what failed, and the next test. Do not close it.
