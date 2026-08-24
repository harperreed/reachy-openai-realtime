<!-- ABOUTME: TDD implementation plan for the five-turn/60-second noise circuit breaker. -->
<!-- ABOUTME: Covers clean session stop, latched sleep, transcript removal, status surfaces, and hardware acceptance. -->

# Noise Rate Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound false-conversation spend by stopping before the fifth turn's OpenAI commit, then latch Reachy asleep until explicit operator action.

**Architecture:** A pure `TurnRateCircuitBreaker` counts local VAD turn completions in a monotonic rolling window. `RealtimeRobotSession` owns it across socket reconnects and returns a distinct noise-bail outcome; `PresenceManager` consumes that outcome and blocks wake-word callbacks until manual wake or app restart. `RuntimeStatus`, the dashboard, and `scripts/robot` expose the latch without logging room speech.

**Tech Stack:** Python 3.14, asyncio/threading, pytest, Ruff, Bash, jq, existing Reachy presence/session state machines.

## Global Constraints

- Trip on the fifth locally completed VAD turn in the half-open window `(now - 60s, now]`.
- Check the breaker before `input_audio_buffer.commit()` and `response.create()`.
- Keep the breaker and 30-minute session lifetime fixed in code; no environment-variable off switch.
- Preserve the turn window across Realtime socket reconnects.
- A noise bail must return `SessionOutcome.NOISE_BAIL`, sleep cleanly, and latch wake words off.
- Manual wake or an explicit app restart rearms wake; manual sleep and connection failure do not latch.
- Remove paid input transcription and the consecutive-wordless classifier without compatibility shims.
- Never log room transcripts or raw microphone audio.
- Never reboot a robot; keep night Reachy stopped until deploy approval.
- Follow TDD for every task: observe the focused test fail before application code changes.
- Run `uv run ruff check . && uv run pytest` before completion.

---

## File map

- Create `reachy_openai_realtime/session/circuit_breaker.py`: pure fixed-policy rolling turn counter and session ceiling constants.
- Create `tests/test_turn_rate_circuit_breaker.py`: boundary and fixed-policy unit tests.
- Modify `reachy_openai_realtime/realtime.py`: call the breaker before commit, mark the noise-bail outcome, enforce the fixed session ceiling, and remove transcript routing.
- Modify `reachy_openai_realtime/main.py`: keep always-on mode stopped after a noise bail instead of constructing another session.
- Modify `reachy_openai_realtime/session/recovery.py`: add `SessionOutcome.NOISE_BAIL`.
- Modify `reachy_openai_realtime/presence/manager.py`: complete `_EitherStop.set()` and own the wake latch.
- Modify `reachy_openai_realtime/runtime_status.py`: publish latch state and reason.
- Modify `reachy_openai_realtime/config.py`: remove obsolete transcript/noise guard settings and environment parsing.
- Modify `reachy_openai_realtime/static/main.js` and `reachy_openai_realtime/static/i18n.js`: render safety sleep distinctly while leaving manual Wake enabled.
- Modify `scripts/robot-ready-state` and `scripts/robot`: recognize and print the latched state.
- Modify focused tests in `tests/test_noise_bail.py`, `tests/test_presence_manager.py`, `tests/test_realtime_config.py`, `tests/test_realtime_manual_turn.py`, `tests/test_runtime_status.py`, `tests/test_robot_script.py`, and `tests/test_static_ui.py`.
- Modify `tests/test_app_loop.py` and `tests/test_realtime_reset.py`: cover always-on bailout and reconnect-window persistence.
- Modify `gotchas.md` and `scenarios.jsonl`: replace the failed guard memory and record hardware acceptance.

---

### Task 1: Pure rolling turn-rate policy

**Files:**
- Create: `reachy_openai_realtime/session/circuit_breaker.py`
- Create: `tests/test_turn_rate_circuit_breaker.py`

**Interfaces:**
- Consumes: monotonic `float` timestamps supplied by the session.
- Produces: `TURN_RATE_LIMIT: Final[int]`, `TURN_RATE_WINDOW_SECONDS: Final[float]`, `SESSION_LIMIT_SECONDS: Final[float]`, and `TurnRateCircuitBreaker.record_turn(now: float) -> bool`.

- [ ] **Step 1: Write failing boundary tests**

Create the test file with the repository's two-line ABOUTME header and these tests:

```python
from reachy_openai_realtime.session.circuit_breaker import (
    SESSION_LIMIT_SECONDS,
    TURN_RATE_LIMIT,
    TURN_RATE_WINDOW_SECONDS,
    TurnRateCircuitBreaker,
)


def test_fixed_safety_limits() -> None:
    assert TURN_RATE_LIMIT == 5
    assert TURN_RATE_WINDOW_SECONDS == 60.0
    assert SESSION_LIMIT_SECONDS == 30 * 60.0


def test_trips_on_fifth_turn_inside_window() -> None:
    breaker = TurnRateCircuitBreaker()
    assert [breaker.record_turn(float(second)) for second in (0, 10, 20, 30)] == [False] * 4
    assert breaker.record_turn(59.999) is True
    assert breaker.turn_count == 5


def test_turn_at_sixty_second_boundary_expires() -> None:
    breaker = TurnRateCircuitBreaker()
    for second in (0, 10, 20, 30):
        assert breaker.record_turn(float(second)) is False
    assert breaker.record_turn(60.0) is False
    assert breaker.turn_count == 4


def test_old_turns_expire_without_transcript_input() -> None:
    breaker = TurnRateCircuitBreaker()
    for second in (1, 2, 3, 4):
        assert breaker.record_turn(float(second)) is False
    assert breaker.record_turn(120.0) is False
    assert breaker.turn_count == 1
```

- [ ] **Step 2: Run the tests and observe the missing-module failure**

Run:

```bash
uv run pytest tests/test_turn_rate_circuit_breaker.py -v
```

Expected: collection fails with `ModuleNotFoundError: reachy_openai_realtime.session.circuit_breaker`.

- [ ] **Step 3: Implement the fixed rolling window**

Create the module with:

```python
# ABOUTME: Fixed safety policy for bounding rapid false user turns.
# ABOUTME: Uses monotonic timestamps only; transcript content cannot reset it.
from __future__ import annotations

from collections import deque
from typing import Final

TURN_RATE_LIMIT: Final[int] = 5
TURN_RATE_WINDOW_SECONDS: Final[float] = 60.0
SESSION_LIMIT_SECONDS: Final[float] = 30 * 60.0


class TurnRateCircuitBreaker:
    def __init__(self) -> None:
        self._turns: deque[float] = deque()

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    def record_turn(self, now: float) -> bool:
        cutoff = now - TURN_RATE_WINDOW_SECONDS
        while self._turns and self._turns[0] <= cutoff:
            self._turns.popleft()
        self._turns.append(now)
        return len(self._turns) >= TURN_RATE_LIMIT
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
uv run pytest tests/test_turn_rate_circuit_breaker.py -v
uv run ruff check reachy_openai_realtime/session/circuit_breaker.py tests/test_turn_rate_circuit_breaker.py
```

Expected: 4 tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 5: Commit the policy unit**

```bash
git add reachy_openai_realtime/session/circuit_breaker.py tests/test_turn_rate_circuit_breaker.py
git commit -m "feat: add turn rate circuit breaker"
```

---

### Task 2: Stop contract and pre-commit session bailout

**Files:**
- Modify: `reachy_openai_realtime/presence/manager.py`
- Modify: `reachy_openai_realtime/session/recovery.py`
- Modify: `reachy_openai_realtime/realtime.py`
- Modify: `reachy_openai_realtime/main.py`
- Modify: `tests/test_presence_manager.py`
- Modify: `tests/test_realtime_manual_turn.py`
- Modify: `tests/test_noise_bail.py`
- Modify: `tests/test_realtime_reconnect.py`
- Modify: `tests/test_realtime_reset.py`
- Modify: `tests/test_app_loop.py`

**Interfaces:**
- Consumes: `TurnRateCircuitBreaker.record_turn(now) -> bool` and the combined stop signal passed by `PresenceManager`.
- Produces: `_EitherStop.set() -> None`, `SessionOutcome.NOISE_BAIL`, and a session that returns that outcome after either safety trip.

- [ ] **Step 1: Write the failing `_EitherStop` contract test**

Import `_EitherStop` in `tests/test_presence_manager.py`, then add:

```python
def test_either_stop_set_targets_session_event_only() -> None:
    app_stop = threading.Event()
    session_stop = threading.Event()
    combined = _EitherStop(app_stop, session_stop)

    combined.set()

    assert combined.is_set() is True
    assert session_stop.is_set() is True
    assert app_stop.is_set() is False
```

Run:

```bash
uv run pytest tests/test_presence_manager.py::test_either_stop_set_targets_session_event_only -v
```

Expected: FAIL with `AttributeError: '_EitherStop' object has no attribute 'set'`.

- [ ] **Step 2: Implement `_EitherStop.set()` and rerun the test**

Add beside `is_set()`:

```python
def set(self) -> None:
    """Request this session to stop without stopping the whole app."""
    self._secondary.set()
```

Run the focused test again. Expected: PASS.

- [ ] **Step 3: Write a failing real record-loop test for the fifth turn**

Extend `FakeStopEvent` in `tests/test_realtime_manual_turn.py` with a real `set()` method:

```python
def set(self) -> None:
    self.stopped = True
```

Extract the existing `test_record_loop_manually_commits_after_local_silence` setup into this helper,
then make the existing test call it:

```python
def _manual_turn_session(
    frames: list[np.ndarray], stop_event: FakeStopEvent
) -> RealtimeRobotSession:
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
    session._pending_wake_audio = None
    session._wake_ready = False
    session._on_session_ready = None
    session._turn_rate_breaker = TurnRateCircuitBreaker()
    session._noise_bailed = False
    session._capture = CaptureWorker(session.robot.media, max_buffer_ms=60_000.0)
    session._mic_ladder = AudioRecoveryLadder()
    return session
```

Add a test that primes four timestamps inside the window, lets the real `_record_loop` observe one
complete VAD turn, and asserts the network boundary was not crossed:

```python
def test_record_loop_fifth_turn_stops_before_commit_or_response() -> None:
    stop_event = FakeStopEvent()
    frames = (
        [stereo_frame(-50.0) for _ in range(10)]
        + [stereo_frame(-30.0) for _ in range(15)]
        + [stereo_frame(-60.0) for _ in range(40)]
    )
    session = _manual_turn_session(frames, stop_event)
    now = time.monotonic()
    for offset in (-4.0, -3.0, -2.0, -1.0):
        assert session._turn_rate_breaker.record_turn(now + offset) is False

    session._capture.start()
    session._audio = session._capture.subscribe("realtime")
    asyncio.run(session._record_loop(stop_event))
    session._capture.close()

    assert stop_event.is_set() is True
    assert session.connection.input_audio_buffer.committed == 0
    assert session.connection.response.created == 0
    assert session._noise_bailed is True
```

Run:

```bash
uv run pytest tests/test_realtime_manual_turn.py::test_record_loop_fifth_turn_stops_before_commit_or_response -v
```

Expected: FAIL because the session has no `_turn_rate_breaker` and still commits the turn.

- [ ] **Step 4: Wire the breaker before camera completion and OpenAI commit**

In `RealtimeRobotSession.__init__`, replace `_noise_turn_count` with:

```python
self._turn_rate_breaker = TurnRateCircuitBreaker()
self._noise_bailed = False
```

At the top of the `decision.stopped` branch, immediately after the `vad.stopped` event and before
the FSM transition, camera completion, input commit, or response request, add:

```python
if self._turn_rate_breaker.record_turn(time.monotonic()):
    self._noise_bailed = True
    self.status.record_event(
        "noise_bail.rate_limit",
        turns=self._turn_rate_breaker.turn_count,
        window_seconds=TURN_RATE_WINDOW_SECONDS,
    )
    stop_event.set()
    return
```

Import the breaker and constants from `session.circuit_breaker`. Add `NOISE_BAIL` to
`SessionOutcome`. At the normal stop return in `run()`, use:

```python
return SessionOutcome.NOISE_BAIL if self._noise_bailed else SessionOutcome.STOPPED
```

Rerun the fifth-turn test and the existing normal-turn test. Expected: both PASS.

- [ ] **Step 5: Replace configurable wall-clock tests with a fixed-ceiling failing test**

Update the supervisor test helper to initialize `_noise_bailed = False`. Replace the configurable
ceiling tests with:

```python
def test_wall_clock_ceiling_marks_noise_bail_and_stops(monkeypatch) -> None:
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    session = _supervisor_session()
    session._session_started_at = time.monotonic() - (SESSION_LIMIT_SECONDS + 1)
    stop_event = threading.Event()

    asyncio.run(_run_supervisor_until_stop(session, stop_event))

    assert stop_event.is_set() is True
    assert session._noise_bailed is True
```

Run the test. Expected: FAIL while `_supervisor_loop` still reads config and does not mark the
outcome.

- [ ] **Step 6: Implement the fixed session ceiling**

Replace the configurable ceiling branch in `_supervisor_loop` with:

```python
if self._session_started_at is not None:
    session_age = time.monotonic() - self._session_started_at
    if session_age >= SESSION_LIMIT_SECONDS:
        self._noise_bailed = True
        self.status.record_event(
            "noise_bail.wall_clock",
            session_age_seconds=round(session_age, 1),
            limit_seconds=SESSION_LIMIT_SECONDS,
        )
        stop_event.set()
        return
```

Keep the existing FSM inactivity branch unchanged. Run:

```bash
uv run pytest tests/test_noise_bail.py tests/test_realtime_manual_turn.py tests/test_realtime_reconnect.py tests/test_presence_manager.py -v
```

Expected: all focused tests pass.

- [ ] **Step 7: Prove reconnect reset preserves the turn window**

Initialize `_turn_rate_breaker` in `tests/test_realtime_reset.py::make_dirty_session`, record one
turn before `reset_connection_state()`, and add this assertion to the existing reset test:

```python
assert session._turn_rate_breaker.turn_count == 1
```

Run:

```bash
uv run pytest tests/test_realtime_reset.py::test_reset_connection_state_clears_spec_checklist -v
```

Expected: PASS because connection reset does not replace the session-owned breaker.

- [ ] **Step 8: Add run-outcome and always-on regressions**

In `tests/test_realtime_reconnect.py`, add a test using the existing real `run()` harness. Set
`session._noise_bailed = True`, provide an already-set stop event, and assert:

```python
assert asyncio.run(session.run(stop_event)) is SessionOutcome.NOISE_BAIL
```

In `tests/test_app_loop.py`, add a session class whose first and only `run()` returns
`SessionOutcome.NOISE_BAIL` and increments a shared construction count. Run `app.run()` with wake
disabled in a thread, wait until the first session returns, set the app stop event, join the thread,
and assert the construction count is exactly one. Expected before the main-loop fix: FAIL because a
second session is constructed.

In the wake-disabled `main.py` outcome branch, add:

```python
if outcome is SessionOutcome.NOISE_BAIL:
    self.runtime_status.set_wake_latch(True, "noise_bail")
    self.runtime_status.set_phase(
        "safety_sleep",
        "Safety sleep is active; restart the app to rearm",
        connected=False,
        event=True,
        detail_key="detail_safety_sleep",
    )
    while not stop_event.is_set():
        stop_event.wait(0.2)
```

This preserves the app process and dashboard while preventing a new always-on session. Run the new
app-loop test and the session-outcome test. Expected: PASS.

- [ ] **Step 9: Commit the session safety path**

Run the focused files, then:

```bash
git add reachy_openai_realtime/presence/manager.py reachy_openai_realtime/session/recovery.py reachy_openai_realtime/realtime.py reachy_openai_realtime/main.py tests/test_presence_manager.py tests/test_realtime_manual_turn.py tests/test_noise_bail.py tests/test_realtime_reconnect.py tests/test_realtime_reset.py tests/test_app_loop.py
git commit -m "fix: stop before runaway response creation"
```

---

### Task 3: Presence wake latch and status contract

**Files:**
- Modify: `reachy_openai_realtime/presence/manager.py`
- Modify: `reachy_openai_realtime/runtime_status.py`
- Modify: `tests/test_presence_manager.py`
- Modify: `tests/test_runtime_status.py`

**Interfaces:**
- Consumes: `SessionOutcome.NOISE_BAIL` from a completed session.
- Produces: `RuntimeStatus.set_wake_latch(latched: bool, reason: str | None)`, status keys `wake_latched` and `wake_latch_reason`, and `PresenceManager.snapshot()` fields with the same names.

- [ ] **Step 1: Write failing runtime-status tests**

Add:

```python
def test_wake_latch_is_exposed_and_can_be_cleared() -> None:
    status = RuntimeStatus()
    assert status.snapshot()["wake_latched"] is False
    assert status.snapshot()["wake_latch_reason"] is None

    status.set_wake_latch(True, "turn_rate")
    assert status.snapshot()["wake_latched"] is True
    assert status.snapshot()["wake_latch_reason"] == "turn_rate"

    status.set_wake_latch(False, None)
    assert status.snapshot()["wake_latched"] is False
    assert status.snapshot()["wake_latch_reason"] is None
```

Run the single test. Expected: FAIL because the method and keys do not exist.

- [ ] **Step 2: Implement the status fields**

Initialize `_wake_latched = False` and `_wake_latch_reason = None` under `RuntimeStatus.__init__`.
Add:

```python
def set_wake_latch(self, latched: bool, reason: str | None) -> None:
    with self._lock:
        self._wake_latched = latched
        self._wake_latch_reason = safe_message(reason) if latched and reason else None
        self._updated_at = _now()
```

Copy both fields into `snapshot()` under the status lock. Rerun the test. Expected: PASS.

- [ ] **Step 3: Write the failing presence lifecycle test**

Import `SessionOutcome` and `WakeEvent`. Make `FakeSession.__init__` accept
`outcome: SessionOutcome = SessionOutcome.STOPPED` and store it. After calling the ready callback,
return immediately when the outcome is `NOISE_BAIL`; otherwise retain the existing loop until the
stop signal and return the configured outcome:

```python
async def run(self, stop_event):
    self.ran = True
    if self._on_session_ready is not None:
        self._on_session_ready()
    if self.outcome is SessionOutcome.NOISE_BAIL:
        return self.outcome
    while not stop_event.is_set():
        await asyncio.sleep(0.01)
    return self.outcome
```

Make `SessionRecorder.__init__` accept
`outcomes: list[SessionOutcome] | None = None`, copy it to `self._outcomes`, and select
`self._outcomes.pop(0)` in `__call__` when available, otherwise `SessionOutcome.STOPPED`. Pass the
selected outcome to `FakeSession`. Existing tests need no arguments. Add this method and field to
`FakeStatus`:

```python
self.wake_latches: list[tuple[bool, str | None]] = []

def set_wake_latch(self, latched: bool, reason: str | None) -> None:
    self.wake_latches.append((latched, reason))
```

Then add:

```python
def test_noise_bail_latches_wake_word_until_manual_wake() -> None:
    capture = FakeCapture()
    factory = SessionRecorder(
        connect=True,
        outcomes=[SessionOutcome.NOISE_BAIL, SessionOutcome.STOPPED],
    )
    status = FakeStatus()
    manager = PresenceManager(
        capture=capture,
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=factory,
        status=status,
    )

    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: manager.snapshot()["wake_latched"] is True)
        assert manager.state is PresenceState.SLEEPING

        manager._on_wake(
            WakeEvent(
                id="ignored",
                detected_at=time.monotonic(),
                phrase="hey reachy",
                score=0.99,
            )
        )
        time.sleep(0.05)
        assert len(factory.sessions) == 1
        assert ("wake.ignored", {"reason": "noise_bail_latched"}) in status.events

        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: len(factory.sessions) == 2)
        assert manager.snapshot()["wake_latched"] is False
```

Run the test. Expected: FAIL because the manager does not consume the session outcome or expose a
latch.

- [ ] **Step 4: Implement latch ownership under the lifecycle lock**

Initialize these fields in `PresenceManager.__init__`:

```python
self._wake_latched = False
self._wake_latch_reason: str | None = None
```

Add them to `snapshot()`. In `_run_session`, initialize
`outcome = SessionOutcome.STOPPED` before `try`, then assign the result of
`asyncio.run(session.run(combined))`. In `finally`, derive
`latched = outcome is SessionOutcome.NOISE_BAIL`, and update the latch and clear `_pending` in the
same lock acquisition:

```python
with self._lock:
    if latched:
        self._wake_latched = True
        self._wake_latch_reason = "noise_bail"
    self._pending = None
    self._session_stop = None
if latched:
    self._status.set_wake_latch(True, "noise_bail")
    self._status.record_event("presence.noise_bail_latched", reason="noise_bail")
```

Do not call status or motion methods while holding `_lock`.

In `_on_wake`, read the latch under `_lock`. If set, do not create `_PendingWake`; outside the lock,
record `wake.ignored` and return. In `request_wake`, clear the latch under `_lock` before arming the
manual wake; after releasing the lock, publish `set_wake_latch(False, None)` and record
`wake.manual_rearm` when a latch was cleared.

- [ ] **Step 5: Prove non-noise outcomes stay wake-armed**

Add tests showing `SessionOutcome.STOPPED` after manual sleep and a failed connection both leave
`wake_latched` false. Run:

```bash
uv run pytest tests/test_presence_manager.py tests/test_runtime_status.py -v
```

Expected: all tests pass with no thread left alive.

- [ ] **Step 6: Commit the latch**

```bash
git add reachy_openai_realtime/presence/manager.py reachy_openai_realtime/runtime_status.py tests/test_presence_manager.py tests/test_runtime_status.py
git commit -m "feat: latch wake after noise bailout"
```

---

### Task 4: Remove transcript guard and paid input transcription

**Files:**
- Modify: `reachy_openai_realtime/config.py`
- Modify: `reachy_openai_realtime/realtime.py`
- Modify: `tests/test_realtime_config.py`
- Modify: `tests/test_noise_bail.py`
- Modify: `gotchas.md`

**Interfaces:**
- Consumes: the fixed breaker and fixed session ceiling from Tasks 1–2.
- Produces: Realtime session input config with no transcription request and no obsolete noise-bail environment controls.

- [ ] **Step 1: Replace transcription tests with a failing absence test**

In `tests/test_realtime_config.py`, change the audio-input assertions to:

```python
def test_session_uses_client_turn_detection_without_paid_input_transcription() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session.motion = _StubMotion()

    audio_input = session._session_config()["audio"]["input"]

    assert audio_input["turn_detection"] is None
    assert audio_input["noise_reduction"] == {"type": "far_field"}
    assert "transcription" not in audio_input
```

Delete tests that construct `AppConfig(input_transcription_model=...)` or assert configurable noise
limits. Add:

```python
def test_obsolete_noise_guard_environment_does_not_change_config(monkeypatch) -> None:
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_NOISE_BAIL_TURNS", "999")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_NOISE_BAIL_SESSION_MINUTES", "999")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_INPUT_TRANSCRIPTION_MODEL", "whisper-1")

    config = AppConfig.from_env()

    assert not hasattr(config, "noise_bail_turns")
    assert not hasattr(config, "noise_bail_session_minutes")
    assert not hasattr(config, "input_transcription_model")
```

Run the two tests. Expected: FAIL because transcription and fields still exist.

- [ ] **Step 2: Remove obsolete application paths**

From `AppConfig`, remove `noise_bail_turns`, `noise_bail_session_minutes`,
`input_transcription_model`, their clamps, and their `from_env()` arguments. From `realtime.py`,
remove:

- `AudioTranscriptionParam` and `re` imports if now unused;
- `_is_garbage_transcript`;
- `_noise_turn_count`;
- the `conversation.item.input_audio_transcription.completed` event branch;
- `_note_user_transcript`; and
- the `_session_config()` transcription block.

Do not remove `RuntimeStatus.record_transcript`; assistant transcripts still use it and `last_user`
remains a stable nullable status field.

- [ ] **Step 3: Delete superseded tests and verify no references remain**

Delete the wordless classifier/counter/event-routing tests from `tests/test_noise_bail.py`. Keep the
fixed wall-clock and FSM-inactivity tests. Run:

```bash
rg -n "noise_bail_turns|noise_bail_session_minutes|input_transcription_model|_is_garbage_transcript|_note_user_transcript|AudioTranscriptionParam" reachy_openai_realtime tests
```

Expected: no matches. Then run:

```bash
uv run pytest tests/test_realtime_config.py tests/test_noise_bail.py -v
uv run ruff check reachy_openai_realtime/config.py reachy_openai_realtime/realtime.py tests/test_realtime_config.py tests/test_noise_bail.py
```

Expected: all focused tests pass and Ruff is clean.

- [ ] **Step 4: Replace stale gotchas with the shipped design**

Edit only the existing anti-runaway entries in `gotchas.md`. Record these facts in plain Markdown:

- transcript classification was removed because word-like noise resets it and transcription costs
  money;
- the fifth local turn in 60 seconds stops before commit/response;
- the 30-minute session ceiling is fixed and reconnect-safe;
- `_EitherStop.set()` targets the session-local event; and
- a noise bail latches wake words until manual wake or app restart.

Remove statements that the old guard is active, configurable, or waiting on a branch.

- [ ] **Step 5: Commit transcript removal**

```bash
git add reachy_openai_realtime/config.py reachy_openai_realtime/realtime.py tests/test_realtime_config.py tests/test_noise_bail.py gotchas.md
git commit -m "refactor: remove transcript noise classifier"
```

---

### Task 5: Surface safety sleep in dashboard and robot CLI

**Files:**
- Modify: `reachy_openai_realtime/static/main.js`
- Modify: `reachy_openai_realtime/static/i18n.js`
- Modify: `scripts/robot-ready-state`
- Modify: `scripts/robot`
- Modify: `tests/test_static_ui.py`
- Modify: `tests/test_robot_script.py`
- Modify: `scenarios.jsonl`

**Interfaces:**
- Consumes: `/api/status` keys `presence`, `wake_latched`, and `wake_latch_reason`.
- Produces: dashboard copy `Safety sleep · use Wake now` and robot modes `connected`, `sleeping`, or `latched`.

- [ ] **Step 1: Write failing parser and static-asset tests**

Add this readiness-parser case:

```python
def test_ready_state_reports_latched_sleep() -> None:
    payload = {
        "connected": False,
        "presence": "sleeping",
        "wake_latched": True,
        "wake_latch_reason": "noise_bail",
        "last_error": None,
    }
    result = subprocess.run(
        [str(READY_STATE)], input=json.dumps(payload), text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "latched"
```

Extend `tests/test_static_ui.py` assertions with `status.wake_latched` and `presence_latched`. Run both
focused tests. Expected: parser returns `sleeping`, and the static assertion fails.

- [ ] **Step 2: Implement dashboard rendering**

Add the i18n row:

```javascript
presence_latched: ["Safety sleep · use Wake now"],
```

Render it ahead of normal presence copy:

```javascript
wakeState.textContent = status.wake_latched
  ? t("presence_latched")
  : presence
    ? t(`presence_${presence}`)
    : t("wake_disabled");
```

Keep the Wake button enabled while presence is `sleeping`; manual wake is the intended rearm.

- [ ] **Step 3: Implement CLI state parsing and output**

In `scripts/robot-ready-state`, check `wake_latched == true` before ordinary sleeping:

```jq
if has("connected") and .connected == true then "connected"
elif has("presence") and .presence == "sleeping"
    and has("wake_latched") and .wake_latched == true
    and has("last_error") and .last_error == null then "latched"
elif has("presence") and .presence == "sleeping"
    and has("last_error") and .last_error == null then "sleeping"
else empty
end
```

Teach both `start_app` and `cmd_status` to print `safety sleep (manual wake required)` for `latched`.
Fetch `/api/status` once per printed app status and pass it through `robot-ready-state`; do not print
the full JSON or any transcript fields.

- [ ] **Step 4: Run focused tests and shell syntax check**

Run:

```bash
uv run pytest tests/test_static_ui.py tests/test_robot_script.py -v
bash -n scripts/robot scripts/robot-ready-state
```

Expected: all tests pass and both scripts parse without output.

- [ ] **Step 5: Record the hardware scenario and commit**

Append one JSON object to `scenarios.jsonl` with:

```json
{"name":"noise-rate-bail-latches-wake","description":"Five local VAD turns inside 60 seconds stop before the fifth response and require manual rearm.","given":"A real Reachy Mini with wake mode enabled and an awake Realtime session","when":"Five short local VAD turns complete inside one rolling 60-second window","then":"The fifth turn creates no response, the session disconnects, status reports wake_latched=true, the wake phrase is ignored, and manual Wake clears the latch","validates":"Runaway conversation spend is bounded independently of transcript content"}
```

Then:

```bash
git add reachy_openai_realtime/static/main.js reachy_openai_realtime/static/i18n.js scripts/robot-ready-state scripts/robot tests/test_static_ui.py tests/test_robot_script.py scenarios.jsonl
git commit -m "feat: expose safety sleep latch"
```

---

### Task 6: Review, canonical verification, and controlled hardware acceptance

**Files:**
- Modify only if review finds a concrete defect in files already listed above.

**Interfaces:**
- Consumes: the complete circuit-breaker feature and repository canonical checks.
- Produces: reviewed commits ready for explicit deploy approval; no automatic push or robot start.

- [ ] **Step 1: Run all focused safety tests together**

```bash
uv run pytest \
  tests/test_turn_rate_circuit_breaker.py \
  tests/test_noise_bail.py \
  tests/test_realtime_manual_turn.py \
  tests/test_realtime_reconnect.py \
  tests/test_presence_manager.py \
  tests/test_runtime_status.py \
  tests/test_realtime_config.py \
  tests/test_robot_script.py \
  tests/test_static_ui.py -v
```

Expected: all selected tests pass with no warnings or leaked threads.

- [ ] **Step 2: Run the canonical gate**

```bash
uv run ruff check . && uv run pytest
```

Expected: Ruff reports `All checks passed!` and the full suite passes with zero failures.

- [ ] **Step 3: Request code review and run fresh-eyes review**

Request a reviewer to compare the implementation against
`docs/superpowers/specs/2026-08-24-noise-rate-circuit-breaker-design.md`. Require explicit findings
for the pre-commit ordering, reconnect persistence, `_EitherStop` contract, latch race, manual rearm,
transcript removal, and status privacy. Fix each confirmed issue with a failing regression test, then
rerun the canonical gate.

- [ ] **Step 4: Commit review fixes, if any**

Stage only reviewed files after `git status`. Use a concise conventional commit that names the fixed
defect. Do not squash or amend earlier TDD commits.

- [ ] **Step 5: Stop at the deployment gate**

Report the branch, commits, test counts, reviewer result, and known weaknesses. Confirm with
`scripts/robot -H 192.168.200.128 status` that night Reachy is still not running. Ask Doctor Biz for
explicit permission before push, deploy, or physical wake.

- [ ] **Step 6: After deploy approval, run hardware acceptance without room transcripts**

Use the night robot at `192.168.200.128`. Start from app stopped. Deploy through the canonical robot
script, verify normal wake-armed sleep, then use manual Wake for the controlled test. Generate five
short VAD turns inside 60 seconds and verify:

```text
noise_bail.rate_limit turns=5 window_seconds=60.0
realtime.disconnected
presence.transition awake -> sleeping
presence.noise_bail_latched reason=noise_bail
```

Verify `/api/status` reports `connected=false`, `presence=sleeping`, and `wake_latched=true`; ask
Doctor Biz to say the wake phrase and verify no connection starts; then use
`POST /api/presence/wake` and verify the latch clears and one new session connects. Manually sleep
through the app and stop the app. Do not call `goto_sleep`, `scripts/robot shutdown`, reboot, or any
other physical lifecycle action without a separate clear yes from Doctor Biz.

- [ ] **Step 7: Record hardware evidence and finish Phase 1 acceptance**

Record only timestamps, event names, counters, states, and commit SHA in the Phase 1 acceptance
artifact. Do not store transcripts or raw audio. Rerun `scripts/robot -H 192.168.200.128 status` and
report its final physical/app state before moving to GitHub phase #23.
