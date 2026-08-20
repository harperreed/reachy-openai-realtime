# Phase 2 Motion Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** All physical movement passes through one priority-arbitrated `MotionManager`, and the Hugging Face emotion/dance libraries become playable through it via `play_emotion`/`stop_emotion`/`play_dance`/`stop_dance` tools.

**Architecture:** Evolve the existing `MotionController` (thread worker, 30 Hz ambient tick, cancel-event) into a `MotionManager` with a single foreground-activity slot arbitrated by the spec §12 priority table, keeping the ambient enable-flag API that `realtime.py` already drives. Recorded moves are played by the manager's own tick loop calling `robot.set_target()` — never the SDK's `play_move`/`cancel_move`, whose cancel path stalls the Wireless microphone. A `RecordedMoveCatalog` wraps the SDK's `RecordedMoves` with background loading, validation, and graceful degradation.

**Tech Stack:** Python 3.10+, threading (no new frameworks), `reachy_mini.motion.recorded_move` (SDK, already installed), `huggingface_hub` (already in the lock as a reachy_mini dependency).

**Spec:** `docs/production-hardening-spec.md` §10 (emotions), §11 (dances), §12 (MotionManager), §29 Phase 2, §30 Motion acceptance, §25 degradation, §18 event names. Tracking issues: #13 (manager), #14 (emotions), #15 (dances).

## Global Constraints

- The Realtime model must never receive direct unrestricted joint-angle control (§12). `robot.set_target`/`robot.goto_target` with validated poses stay the only motor interface.
- NEVER call `ReachyMini.cancel_move()` or `media.stop_playing()` from app code: on Wireless, mic and speaker share one GStreamer pipeline and both calls stall the microphone (gotchas.md). This is why the manager plays recorded moves itself.
- Never store or return the OpenAI API key; all recorder output goes through `observability/events.py:redact_secrets`.
- Do not introduce Hermes, OpenClaw, LangChain, or a second conversational LLM (§31).
- Python floor is 3.10 (no `asyncio.timeout()`, no `StrEnum` — `IntEnum` is fine). Ruff line length 110.
- Canonical check after every task: `uv run ruff check . && uv run pytest`. Dev tools live in `[dependency-groups] dev`; `uv run which ruff` must point into `.venv`.
- Flight-recorder event names come from spec §18 only: `motion.started`, `motion.completed`, `motion.cancelled`, `emotion.started`, `emotion.completed`, `dance.started`, `dance.completed`. Do not invent new event names.
- Tool descriptions and user-facing strings follow the existing Japanese house style in `TOOL_DEFINITIONS`; error strings returned to the model stay English (existing convention: "invalid look direction").
- Unit tests must not touch the network. The catalog takes an injectable loader; tests inject fakes at the SDK boundary only — never mock our own logic.
- No giant refactor commits (§31): each task lands green on `uv run ruff check . && uv run pytest`.
- Ambiguity resolves in spec order: robustness > conversational latency > motion safety > simplicity > features.
- App stop is SIGINT with a ~20 s SIGKILL deadline: every thread join stays bounded (existing `close()` uses `join(timeout=2.0)`).

## Research notes (verified 2026-08-19 against the installed reachy_mini SDK)

These are load-bearing facts. Do not re-derive them from memory; they were read from
`.venv/lib/python3.14/site-packages/reachy_mini/motion/recorded_move.py` and `reachy_mini/reachy_mini.py`.

- `reachy_mini.motion.recorded_move.RecordedMoves(hf_dataset_name: str)` downloads/loads a HF **dataset** (cache-first via `snapshot_download(..., repo_type="dataset", local_files_only=True)`, falling back to a network download). Its `__init__` does blocking I/O and `json.load`s every move — call it off the hot path. API: `.list_moves() -> List[str]`, `.get(move_name) -> RecordedMove` (raises `ValueError` for unknown names).
- `RecordedMove` API: `.duration -> float` (seconds), `.sound_path -> Optional[Path]`, `.description: str`, `.evaluate(t) -> (head 4x4 np.ndarray, antennas np.ndarray in RADIANS, body_yaw float in radians)`. **`evaluate` raises beyond the last timestamp** — the SDK's own player clamps with `t = min(elapsed, move.duration - 1e-2)`. Do the same.
- Dataset names (SDK constant `DEFAULT_DATASETS`): `pollen-robotics/reachy-mini-emotions-library`, `pollen-robotics/reachy-mini-dances-library`. The robot's daemon preloads both at startup, so on-robot cache is warm; a dev box's first load may hit the network — hence background loading.
- `ReachyMini.async_play_move` plays sidecar sounds via `media_manager.play_sound(...)` and its cancel path is `cancel_move()` → `media_manager.stop_playing()` → **mic stall on Wireless**. We therefore play moves ourselves and skip sidecar sounds in Phase 2 (see Notes).
- Existing motion worker (`MotionController._worker`) ticks at `self._idle_period = 1/30` s and already implements: base-head persistence (`look` retargets the ambient base pose), cancel-event checks between goto segments, return-to-base transitions after ambient motions, bounded `close()`.
- `realtime.py` drives ambient state via `set_listening_enabled` / `set_speaking_enabled` / `set_idle_enabled` at realtime.py:308–311, 454–455, 524, 531, 561, 776–779, 859–861, 974, 997; barge-in/interruption and reset call `motion.stop_current()` (realtime.py:832, 1075, 1164). Tool calls arrive at `_handle_tool_call` → `self.motion.submit(name, arguments)` (realtime.py:1011); `ValueError`/`TypeError` from `submit` become `{"ok": False, "error": str(exc)}` for the model.
- Session config: `session.update` sends `tools=TOOL_DEFINITIONS` (realtime.py:342) and `instructions=session_instructions(language_code)` (realtime.py:330) once per connection; sessions reconnect at least hourly (60-min server cap), so per-connection tool/instruction snapshots refresh naturally.
- Motion health today is static: `set_component_health("motion", True, expires=False)` at main.py:323, flipped to `False` at main.py:418 on teardown. Issue #13 requires a live heartbeat instead (issue #10's supervisor hook). `RuntimeStatus.set_component_health(name, ok, *, now=..., expires=True)` with expiry marks the component stale after 10 s without a beat (see tests/test_runtime_status.py health tests).

## Spec priority table (§12) → `MotionPriority`

```text
emergency/stop             100   STOP        (cancellation event, not a motion)
barge-in cancellation       90   BARGE_IN    (cancellation event, not a motion)
explicit tool gesture       75   GESTURE     (nod, shake_head, express)
HF emotion                  70   EMOTION
HF dance                    65   DANCE
look-at-speaker             50   —           (Phase 3, needs DoA — issue #9; value reserved)
look                        45   LOOK
speaking motion             20   SPEAKING    (background enable)
listening motion            15   LISTENING   (background enable)
idle breathing              10   IDLE        (background enable)
```

Arbitration rules (decisions locked here, cite them instead of re-deciding):

1. One foreground activity at a time; no queue. A new submission with priority **>= the effective current priority** preempts (cancels current, becomes pending); strictly lower priority is rejected with `{"ok": False, "error": "busy: <name> is active at priority <N>"}`. This replaces the old FIFO queue (maxsize 8) — deterministic preemption is what §12 asks for; a scheduler is YAGNI.
2. Backgrounds (idle/listening/speaking) are enable flags exactly as today, evaluated only when no foreground activity is running or pending — that is §12's "recorded moves temporarily suppress background speaking/idle motions". `submit()` no longer clears the enable flags (today's lines 281–283); the flags survive the activity so the worker resumes the right background automatically — §12's "transition smoothly back".
3. `stop_motion` (STOP) and barge-in (`stop_current()`, BARGE_IN) are cancellation events: they cancel any foreground activity and clear the background enables (existing `stop_current` semantics kept for realtime.py's call sites). Stop always preempts (§30).
4. After any foreground activity ends (completed or cancelled), the worker gotos back to `base_head` with neutral antennas before background evaluation resumes. `look` keeps its existing behavior of re-basing `base_head`, so ambient motion keeps orbiting the new gaze direction.
5. Recorded moves may include `body_yaw`; pass it through to `set_target` for recorded moves only (they are Pollen-authored trajectories). Scripted gestures keep `body_yaw=None` as today.
6. Event emission: scripted gestures and look emit `motion.started/completed/cancelled`; recorded moves emit `emotion.*`/`dance.*` (`*.started`, `*.completed`, and `motion.cancelled` when cut short — §18 has no `emotion.cancelled`). One family per activity, no doubles.

## File structure

```text
reachy_openai_realtime/
    motion/                      # replaces motion.py (635 lines) — Task 1
        __init__.py              # re-exports the public surface (import sites keep working)
        builtin.py               # IdleBreathingMotion, ListeningNodMotion, SpeakingMotion (moved verbatim)
        manager.py               # MotionPriority, MotionCommand, ReachyMotionAPI, MotionManager
        recorded_moves.py        # RecordedMoveCatalog, dataset constants — Task 4
        tools.py                 # TOOL_DEFINITIONS (moved) + recorded-move tool defs — Tasks 1, 6
    config.py                    # + recorded_moves_instructions() — Task 7
    main.py                      # catalog construction, heartbeat wiring — Tasks 3, 7
    realtime.py                  # dynamic tools + instruction context — Task 7
tests/
    test_motion.py               # updated imports/rename; arbitration tests — Tasks 1, 2
    test_recorded_moves.py       # catalog tests — Task 4
    test_motion_playback.py      # recorded-move playback tests — Task 5
    test_motion_tools.py         # tool routing tests — Task 6
```

Deviation from spec §28 noted deliberately: §28 suggests separate `emotions.py`/`dances.py`; one parameterized `RecordedMoveCatalog` with two instances is simpler and sufficient (YAGNI). §28 is "suggested", and §31 forbids refactor-for-its-own-sake. `tools/` package and `ToolExecutor` stay Phase 3 (#21).

---

### Task 1: Restructure `motion.py` into the `motion/` package and rename to `MotionManager`

Pure mechanical restructuring, two commits, zero behavior change. The green suite is the proof.

**Files:**
- Create: `reachy_openai_realtime/motion/__init__.py`, `motion/builtin.py`, `motion/manager.py`, `motion/tools.py`
- Delete: `reachy_openai_realtime/motion.py`
- Modify: `reachy_openai_realtime/realtime.py:40,147`, `reachy_openai_realtime/main.py:19,313`
- Modify: `tests/test_motion.py`, `tests/test_supervisor.py:116,126,201,215`, `tests/test_app_loop.py:45,50` (comments)

**Interfaces:**
- Consumes: current `motion.py` contents (move verbatim; do not edit logic while moving).
- Produces: `from reachy_openai_realtime.motion import MotionManager, TOOL_DEFINITIONS, IdleBreathingMotion, ListeningNodMotion, SpeakingMotion, MotionCommand, ReachyMotionAPI, Direction, Emotion` — the exact import surface later tasks and existing code rely on.

- [ ] **Step 1: Split the module (commit 1).** Create the package:
  - `motion/builtin.py`: `IdleBreathingMotion`, `ListeningNodMotion`, `SpeakingMotion` classes verbatim (motion.py:47–187) plus the imports they need (`numpy`, `create_head_pose`, `linear_pose_interpolation`, `Any`).
  - `motion/tools.py`: the `TOOL_DEFINITIONS` list verbatim (motion.py:190–251).
  - `motion/manager.py`: `Direction`, `Emotion`, `ReachyMotionAPI`, `MotionCommand`, `MotionController` verbatim (motion.py:16–44, 254–635), importing the builtin classes from `.builtin`.
  - `motion/__init__.py`:

```python
# ABOUTME: Motion package public surface — arbitration manager, ambient generators,
# ABOUTME: and the Realtime tool definitions for physical movement.
from .builtin import IdleBreathingMotion, ListeningNodMotion, SpeakingMotion
from .manager import Direction, Emotion, MotionCommand, MotionController, ReachyMotionAPI
from .tools import TOOL_DEFINITIONS

__all__ = [
    "Direction",
    "Emotion",
    "IdleBreathingMotion",
    "ListeningNodMotion",
    "MotionCommand",
    "MotionController",
    "ReachyMotionAPI",
    "SpeakingMotion",
    "TOOL_DEFINITIONS",
]
```

  Delete `motion.py` in the same commit. No other file changes: every existing import (`from .motion import TOOL_DEFINITIONS, MotionController`, test imports) resolves against the package `__init__`.

- [ ] **Step 2: Run the full check** — `uv run ruff check . && uv run pytest`. Expected: all 175 tests pass unchanged. If anything fails, the move was not verbatim; fix the move, do not "fix" tests.

- [ ] **Step 3: Commit 1** — `git add -A reachy_openai_realtime/motion reachy_openai_realtime/motion.py && git commit -m "refactor: split motion.py into motion/ package"` (run `git status` first per house rules).

- [ ] **Step 4: Rename `MotionController` → `MotionManager` (commit 2).** The spec names the arbiter `MotionManager` (§12). Rename the class in `motion/manager.py`, update `motion/__init__.py` (export `MotionManager`; do NOT keep a `MotionController = MotionManager` alias — dual names are backward compat, which needs Harper's approval and has no consumer here), and update every reference found by the rename-safety sweep:
  - direct refs: `realtime.py:40,147`, `main.py:19,313`, `tests/test_motion.py` (imports + ~9 call sites), `tests/test_supervisor.py:116,126,201,215`
  - comments/docstrings: `tests/test_app_loop.py:45,50` ("MotionController protocol" comment), any docstring inside `manager.py`
  - string literals and dynamic imports: `grep -rn "MotionController" --include="*.py" .` must return zero hits afterward; also grep `docs/` is NOT updated (the spec stays verbatim per house rule — annotate in plans, never edit the spec).

- [ ] **Step 5: Run the full check** — `uv run ruff check . && uv run pytest`. Expected: green, same test count.

- [ ] **Step 6: Commit 2** — `git commit -am "refactor: rename MotionController to MotionManager per spec §12"`.

---

### Task 2: Priority arbitration — foreground activity slot, preemption, §18 motion events

The behavioral core of issue #13. Replaces the FIFO queue with a single arbitrated slot.

**Files:**
- Modify: `reachy_openai_realtime/motion/manager.py`
- Modify: `reachy_openai_realtime/motion/__init__.py` (export `MotionPriority`)
- Test: `tests/test_motion.py`

**Interfaces:**
- Consumes: Task 1's `MotionManager` (worker thread, `_execute`, ambient updates, `_cancel_event`).
- Produces (later tasks rely on these exact names):
  - `class MotionPriority(IntEnum)` with members `STOP = 100`, `BARGE_IN = 90`, `GESTURE = 75`, `EMOTION = 70`, `DANCE = 65`, `LOOK = 45`, `SPEAKING = 20`, `LISTENING = 15`, `IDLE = 10`
  - `MotionManager.attach_recorder(record: Callable[..., None]) -> None` — accepts `RuntimeStatus.record_event`-shaped callables (`record(event_name, **fields)`); never raises if unset
  - `MotionManager.submit(name, arguments) -> dict` — unchanged signature; new rejection shape `{"ok": False, "error": "busy: <name> is active at priority <N>"}`
  - `MotionManager._start_activity(name: str, priority: int, run: Callable[[], None], kind: str = "motion") -> dict` — internal seam Task 5 reuses for recorded moves (`kind` selects the §18 event family)
  - `MotionManager.stop_current(reason: str = "stop") -> None` — existing callers pass no argument and keep working

- [ ] **Step 1: Write the failing tests.** Replace the queue-era tests in `tests/test_motion.py` that assert FIFO behavior (keep every validation test and the ambient-motion tests untouched) and add:

```python
import threading
import time

from reachy_openai_realtime.motion import MotionManager, MotionPriority


class RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_lower_priority_submission_is_rejected_while_higher_runs() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    release = threading.Event()
    started = threading.Event()

    def slow_gesture() -> None:
        started.set()
        release.wait(timeout=2.0)

    accepted = manager._start_activity("nod", MotionPriority.GESTURE, slow_gesture)
    assert accepted == {"ok": True, "motion": "nod"}
    manager.start()
    assert started.wait(timeout=2.0)

    rejected = manager.submit("look", {"direction": "left"})
    assert rejected["ok"] is False
    assert "busy" in rejected["error"] and "priority 75" in rejected["error"]
    release.set()
    manager.close()


def test_equal_or_higher_priority_preempts_running_activity() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    first_started = threading.Event()
    first_cancelled = threading.Event()

    def first() -> None:
        first_started.set()
        # cooperative activity: exits promptly once preempted
        while not manager._cancel_event.is_set():
            time.sleep(0.005)
        first_cancelled.set()

    manager._start_activity("shake_head", MotionPriority.GESTURE, first)
    manager.start()
    assert first_started.wait(timeout=2.0)

    result = manager.submit("nod", {"count": 1})
    assert result["ok"] is True
    assert first_cancelled.wait(timeout=2.0)
    assert wait_until(lambda: ("motion.cancelled", {"motion": "shake_head", "reason": "preempted"}) in recorder.events)
    assert wait_until(lambda: any(e == "motion.completed" and f.get("motion") == "nod" for e, f in recorder.events))
    manager.close()


def test_stop_motion_cancels_and_always_wins() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    started = threading.Event()

    def running() -> None:
        started.set()
        while not manager._cancel_event.is_set():
            time.sleep(0.005)

    manager._start_activity("nod", MotionPriority.GESTURE, running)
    manager.start()
    assert started.wait(timeout=2.0)
    result = manager.submit("stop_motion", {})
    assert result == {"ok": True, "motion": "stop_motion"}
    assert wait_until(lambda: ("motion.cancelled", {"motion": "nod", "reason": "stop"}) in recorder.events)
    manager.close()


def test_background_enables_survive_a_foreground_activity() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.set_idle_enabled(True)
    result = manager.submit("nod", {"count": 1})
    assert result["ok"] is True
    # §12: recorded/explicit moves suppress background motion but do not clear it
    assert manager._idle_enabled.is_set()
    manager.close()


def test_motion_events_emitted_for_gesture_lifecycle() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    manager.submit("nod", {"count": 1})
    assert wait_until(lambda: any(e == "motion.completed" and f.get("motion") == "nod" for e, f in recorder.events))
    names = [e for e, _ in recorder.events]
    assert names.index("motion.started") < names.index("motion.completed")
    manager.close()


def test_recorder_absence_does_not_break_motion() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)  # no recorder attached
    manager.start()
    assert manager.submit("nod", {"count": 1})["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    manager.close()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_motion.py -x -q`. Expected: `ImportError: cannot import name 'MotionPriority'`.

- [ ] **Step 3: Implement in `motion/manager.py`.** The shape (full logic, not pseudocode — adapt names to the file as moved):

```python
class MotionPriority(IntEnum):
    """Spec §12 arbitration table. Higher preempts lower; 50 is reserved for look_at_speaker (Phase 3)."""

    STOP = 100
    BARGE_IN = 90
    GESTURE = 75
    EMOTION = 70
    DANCE = 65
    LOOK = 45
    SPEAKING = 20
    LISTENING = 15
    IDLE = 10


@dataclass
class _Activity:
    name: str
    priority: int
    run: Callable[[], None]
    kind: str = "motion"  # "motion" | "emotion" | "dance" — selects the §18 event family
```

  In `MotionManager.__init__`: replace `self._queue` with `self._slot_lock = threading.Lock()`, `self._slot_cv = threading.Condition(self._slot_lock)`, `self._pending: _Activity | None = None`, `self._current: _Activity | None = None`, `self._record: Callable[..., None] | None = None`. Keep `_cancel_event`, `_stop_event`, the enable events, base-head state, and `_idle_period` exactly as they are.

```python
def attach_recorder(self, record: Callable[..., None]) -> None:
    self._record = record

def _emit(self, event: str, **fields: Any) -> None:
    if self._record is None:
        return
    try:
        self._record(event, **fields)
    except Exception:
        logger.debug("motion event emission failed", exc_info=True)

def _start_activity(self, name: str, priority: int, run: Callable[[], None], kind: str = "motion") -> dict[str, Any]:
    # Check-and-set under ONE lock hold — a split check/act races the worker clearing _current.
    with self._slot_cv:
        blocking = max(
            (a for a in (self._current, self._pending) if a is not None),
            key=lambda a: a.priority,
            default=None,
        )
        if blocking is not None and priority < blocking.priority:
            return {"ok": False, "error": f"busy: {blocking.name} is active at priority {blocking.priority}"}
        if self._current is not None:
            self._cancel_reason = "preempted"
            self._cancel_event.set()
        self._pending = _Activity(name, priority, run, kind)
        self._slot_cv.notify()
    return {"ok": True, "motion": name}
```

  `submit(name, arguments)`: keep `validate()` first (unchanged static shape validation). Then route:
  - `stop_motion` → `self.stop_current(reason="stop")`, return `{"ok": True, "motion": "stop_motion"}`
  - `look` → `_start_activity("look", MotionPriority.LOOK, lambda: self._execute(command))`
  - `nod` / `shake_head` / `express` → `_start_activity(name, MotionPriority.GESTURE, lambda: self._execute(command))`
  - on acceptance for gestures, return `{"ok": True, "motion": name, "arguments": command.arguments}` (existing shape — augment `_start_activity`'s dict). **Remove** the three `set_*_enabled(False)` calls from `submit` (arbitration rule 2).

  `stop_current(reason: str = "stop")`: keep existing semantics (clear all three enables, set `_cancel_event`) but instead of draining the queue, clear `self._pending` under the lock, and remember `self._cancel_reason = reason` for the worker's cancelled-event emission. `realtime.py`'s no-arg calls mean barge-in and reset keep working unchanged; the `reason` lands in the event payload. (Barge-in call sites pass nothing and get `"stop"`; distinguishing "barge_in" in the payload is wired in Task 7's realtime change — see that task.) PRESERVE the existing four-line comment explaining why `ReachyMini.cancel_move()` is never called (motion.py:361–364, the GStreamer mic-stall rationale) — it stays true and load-bearing; reword only the queue-drain sentence. `close()` calls `self.stop_current(reason="shutdown")` so a cut-short activity's `motion.cancelled` says why.

  `_worker` loop:

```python
def _worker(self) -> None:
    while not self._stop_event.is_set():
        with self._slot_cv:
            if self._pending is None:
                self._slot_cv.wait(timeout=self._idle_period)
            activity, self._pending = self._pending, None
            if activity is not None:
                self._current = activity
        if activity is None:
            if not self._has_foreground():
                self._update_ambient_motion()
            continue
        self._reset_ambient_generators()   # existing field-clearing block from the old queue branch
        self._cancel_event.clear()
        self._cancel_reason = "stop"
        started_at = time.monotonic()
        family = activity.kind  # "motion" | "emotion" | "dance"
        label_key = {"motion": "motion", "emotion": "emotion", "dance": "dance"}[family]
        self._emit(f"{family}.started", **{label_key: activity.name, "priority": activity.priority})
        try:
            activity.run()
        except Exception:
            logger.exception("Motion failed: %s", activity.name)
        duration_ms = round((time.monotonic() - started_at) * 1000.0, 1)
        if self._cancel_event.is_set():
            self._emit("motion.cancelled", motion=activity.name, reason=self._cancel_reason)
        else:
            self._emit(f"{family}.completed", **{label_key: activity.name, "duration_ms": duration_ms})
        with self._slot_lock:
            self._current = None
            has_pending = self._pending is not None
        if not has_pending:
            self._return_to_base()  # a preempting activity is about to move anyway — skip the detour
        self._last_activity_at = time.monotonic()
```

  Helpers: `_has_foreground()` checks current/pending under the lock; `_reset_ambient_generators()` is the existing block that nulls `_idle_motion`/`_listening_motion`/`_speaking_motion` state (motion.py:404–410 as moved); `_return_to_base()` gotos `self._get_base_head()` with `antennas=np.deg2rad([-10.0, 10.0])`, `duration=0.4`, `body_yaw=None`, wrapped in try/except like the existing return transitions. Emit for `emotion.started`/`dance.started` uses the key `emotion=`/`dance=` respectively (spec §18 families; Task 5 exercises them). Update `close()` to notify the condition variable so the worker exits promptly, keeping `join(timeout=2.0)`.

- [ ] **Step 4: Run the full check** — `uv run ruff check . && uv run pytest`. All green, including untouched ambient tests (they call `_update_ambient_motion` paths indirectly).

- [ ] **Step 5: Commit** — `git commit -am "feat: priority arbitration with preemption in MotionManager (closes-ref #13 core)"` (plain message; the `Closes #13` footer belongs to the branch-final commit in Task 8).

---

### Task 3: Motion heartbeat → live `/api/health` component

Issue #13's supervisor hook (from #10): the worker loop proves liveness; a dead worker goes stale within 10 s. This task also wires the manager's flight-recorder callback in `main.py` (created in Task 2, unconnected until now).

**Files:**
- Modify: `reachy_openai_realtime/motion/manager.py` (heartbeat callback)
- Modify: `reachy_openai_realtime/main.py:313–323, 417–418`
- Test: `tests/test_motion.py`, `tests/test_health.py`

**Interfaces:**
- Consumes: Task 2's worker loop; `RuntimeStatus.set_component_health(name, ok, *, now=..., expires=True)` (existing).
- Produces: `MotionManager.set_heartbeat(callback: Callable[[], None]) -> None`; the worker invokes it once per loop iteration (ambient tick and after each activity), exceptions swallowed with `logger.debug`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_motion.py`):

```python
def test_worker_loop_beats_heartbeat() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    beats: list[float] = []
    manager.set_heartbeat(lambda: beats.append(time.monotonic()))
    manager.start()
    assert wait_until(lambda: len(beats) >= 3)
    manager.close()


def test_heartbeat_exception_does_not_kill_worker() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)

    def broken() -> None:
        raise RuntimeError("health sink down")

    manager.set_heartbeat(broken)
    manager.start()
    assert manager.submit("nod", {"count": 1})["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    manager.close()
```

  And in `tests/test_health.py`, extend the existing health coverage: motion health must go stale without beats — construct `RuntimeStatus`, call `set_component_health("motion", True, now=100.0)` (expiring), assert `health(now=111.0)["motion"] is False`. (If an equivalent staleness test already covers the "motion" key, adapt rather than duplicate.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_motion.py -q`. Expected: `AttributeError: ... set_heartbeat`.

- [ ] **Step 3: Implement.** In `manager.py`: `self._heartbeat: Callable[[], None] | None = None`, setter, and a `_beat()` helper called at the top of every `_worker` iteration and once after each activity completes:

```python
def set_heartbeat(self, callback: Callable[[], None]) -> None:
    self._heartbeat = callback

def _beat(self) -> None:
    if self._heartbeat is None:
        return
    try:
        self._heartbeat()
    except Exception:
        logger.debug("motion heartbeat callback failed", exc_info=True)
```

  In `main.py`: replace the static line 323 with heartbeat AND flight-recorder wiring **before** `motion.start()` (without the `attach_recorder` line, Task 2's §18 events never leave the process — the manager's recorder is unset in production):

```python
motion.attach_recorder(self.runtime_status.record_event)
motion.set_heartbeat(lambda: self.runtime_status.set_component_health("motion", True))
motion.start()
```

  (`RuntimeStatus.record_event(event, **fields)` — runtime_status.py:71 — forwards to the attached flight recorder, which redacts via `observability/events.py:redact_secrets`.) Keep the teardown `set_component_health("motion", False, expires=False)` at main.py:418 — an explicit false on shutdown beats waiting for staleness.

- [ ] **Step 4: Run the full check** — `uv run ruff check . && uv run pytest`.

- [ ] **Step 5: Commit** — `git commit -am "feat: motion worker heartbeat drives /api/health motion component"`.

---

### Task 4: `RecordedMoveCatalog` — load, degrade, validate, enumerate

Issue #14's abstraction (shared by #15). Wraps the SDK's `RecordedMoves` behind a network-safe, thread-safe catalog.

**Files:**
- Create: `reachy_openai_realtime/motion/recorded_moves.py`
- Modify: `reachy_openai_realtime/motion/__init__.py` (export `RecordedMoveCatalog`, `EMOTIONS_DATASET`, `DANCES_DATASET`)
- Test: `tests/test_recorded_moves.py`

**Interfaces:**
- Consumes: `reachy_mini.motion.recorded_move.RecordedMoves` (SDK, injectable).
- Produces (Tasks 5–7 rely on these exact names):
  - `EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"`, `DANCES_DATASET = "pollen-robotics/reachy-mini-dances-library"`
  - `class RecordedMoveCatalog:`
    - `__init__(self, dataset: str, *, loader: Callable[[str], Any] | None = None) -> None` — `loader` defaults to `RecordedMoves`; tests inject fakes
    - `load_async(self) -> None` — one background attempt, daemon thread, idempotent
    - `wait_ready(self, timeout: float) -> bool` — for tests and optional startup logging
    - `state -> str` property: `"loading" | "ready" | "unavailable"` (plain strings; 3.10 floor, no StrEnum)
    - `available -> bool` property (`state == "ready"`)
    - `names(self) -> list[str]` — sorted, **sanitized** (see below), `[]` unless ready
    - `get(self, name: str) -> Any` — the SDK `RecordedMove`; raises `ValueError` with the dataset name for unknown/unsanitary names; raises `RuntimeError("catalog not ready")` if not ready
- Sanitization (prompt-injection hygiene — catalog names flow into session instructions in Task 7): only names matching `^[A-Za-z0-9 _\-]{1,64}$` are exposed by `names()` or playable via `get()`; others are dropped with one `logger.warning` per load listing the excluded names.

- [ ] **Step 1: Write the failing tests:**

```python
# ABOUTME: Tests for RecordedMoveCatalog — background loading, degradation,
# ABOUTME: name sanitization, and validation against the live catalog.
import threading

import pytest

from reachy_openai_realtime.motion import DANCES_DATASET, EMOTIONS_DATASET, RecordedMoveCatalog


class FakeRecordedMoves:
    """Stands in for reachy_mini.motion.recorded_move.RecordedMoves at the SDK boundary."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_moves(self) -> list[str]:
        return list(self._names)

    def get(self, name: str):
        if name not in self._names:
            raise ValueError(f"Move {name} not found")
        return object()


def test_dataset_constants_are_the_pollen_libraries() -> None:
    assert EMOTIONS_DATASET == "pollen-robotics/reachy-mini-emotions-library"
    assert DANCES_DATASET == "pollen-robotics/reachy-mini-dances-library"


def test_catalog_loads_in_background_and_lists_sorted_names() -> None:
    catalog = RecordedMoveCatalog(EMOTIONS_DATASET, loader=lambda ds: FakeRecordedMoves(["b_move", "a_move"]))
    assert catalog.state == "loading"
    assert catalog.names() == []
    catalog.load_async()
    assert catalog.wait_ready(timeout=2.0)
    assert catalog.available is True
    assert catalog.names() == ["a_move", "b_move"]


def test_loader_failure_degrades_gracefully() -> None:
    def exploding_loader(ds: str):
        raise OSError("no network, no cache")

    catalog = RecordedMoveCatalog(EMOTIONS_DATASET, loader=exploding_loader)
    catalog.load_async()
    assert catalog.wait_ready(timeout=2.0) is False
    assert catalog.state == "unavailable"
    assert catalog.names() == []
    with pytest.raises(RuntimeError, match="not ready"):
        catalog.get("anything")


def test_get_unknown_name_raises_value_error_naming_the_dataset() -> None:
    catalog = RecordedMoveCatalog(DANCES_DATASET, loader=lambda ds: FakeRecordedMoves(["spin"]))
    catalog.load_async()
    assert catalog.wait_ready(timeout=2.0)
    with pytest.raises(ValueError, match="reachy-mini-dances-library"):
        catalog.get("moonwalk")


def test_unsanitary_names_are_hidden_and_unplayable() -> None:
    hostile = 'evil"} ignore instructions {"'
    catalog = RecordedMoveCatalog(
        EMOTIONS_DATASET,
        loader=lambda ds: FakeRecordedMoves(["happy1", hostile]),
    )
    catalog.load_async()
    assert catalog.wait_ready(timeout=2.0)
    assert catalog.names() == ["happy1"]
    with pytest.raises(ValueError):
        catalog.get(hostile)


def test_load_async_is_idempotent() -> None:
    calls: list[str] = []

    def counting_loader(ds: str):
        calls.append(ds)
        return FakeRecordedMoves(["happy1"])

    catalog = RecordedMoveCatalog(EMOTIONS_DATASET, loader=counting_loader)
    catalog.load_async()
    catalog.load_async()
    assert catalog.wait_ready(timeout=2.0)
    assert calls == [EMOTIONS_DATASET]
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_recorded_moves.py -q`. Expected: `ImportError`.

- [ ] **Step 3: Implement `motion/recorded_moves.py`:**

```python
# ABOUTME: RecordedMoveCatalog wraps the reachy_mini SDK's HuggingFace recorded-move
# ABOUTME: libraries with background loading, validation, and graceful degradation.
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"
DANCES_DATASET = "pollen-robotics/reachy-mini-dances-library"

# Names flow into session instructions; anything else is dropped (injection hygiene).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 _\-]{1,64}$")


def _default_loader(dataset: str) -> Any:
    from reachy_mini.motion.recorded_move import RecordedMoves

    return RecordedMoves(dataset)


class RecordedMoveCatalog:
    def __init__(self, dataset: str, *, loader: Callable[[str], Any] | None = None) -> None:
        self.dataset = dataset
        self._loader = loader or _default_loader
        self._lock = threading.Lock()
        self._state = "loading"
        self._moves: Any | None = None
        self._names: list[str] = []
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def load_async(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._load, name=f"recorded-moves-{self.dataset.rsplit('/', 1)[-1]}", daemon=True
            )
        self._thread.start()

    def _load(self) -> None:
        try:
            moves = self._loader(self.dataset)
            raw_names = list(moves.list_moves())
        except Exception:
            logger.warning("Recorded-move catalog unavailable: %s", self.dataset, exc_info=True)
            with self._lock:
                self._state = "unavailable"
            self._done.set()
            return
        safe = sorted(name for name in raw_names if _SAFE_NAME.match(name))
        dropped = sorted(set(raw_names) - set(safe))
        if dropped:
            logger.warning("Dropped %d unsanitary move names from %s: %s", len(dropped), self.dataset, dropped)
        with self._lock:
            self._moves = moves
            self._names = safe
            self._state = "ready"
        self._done.set()

    def wait_ready(self, timeout: float) -> bool:
        self._done.wait(timeout=timeout)
        return self.available

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def available(self) -> bool:
        return self.state == "ready"

    def names(self) -> list[str]:
        with self._lock:
            return list(self._names)

    def get(self, name: str) -> Any:
        with self._lock:
            if self._state != "ready" or self._moves is None:
                raise RuntimeError(f"catalog not ready: {self.dataset}")
            if name not in self._names:
                raise ValueError(f"unknown move {name!r} in {self.dataset}")
            moves = self._moves
        return moves.get(name)
```

  (Note `get()` calls the SDK outside the lock; `RecordedMoves.get` is a dict lookup + object construction, no I/O — all JSON was loaded in `_load`.) Export the three names from `motion/__init__.py` and add them to `__all__`.

- [ ] **Step 4: Run the full check** — `uv run ruff check . && uv run pytest`.

- [ ] **Step 5: Commit** — `git commit -am "feat: RecordedMoveCatalog with background load and degradation"`.

---

### Task 5: Recorded-move playback through the manager

The manager plays `RecordedMove` trajectories in its own loop — initial goto, clamped `evaluate`, `body_yaw` pass-through, cancellation, §18 `emotion.*`/`dance.*` events.

**Files:**
- Modify: `reachy_openai_realtime/motion/manager.py`
- Test: `tests/test_motion_playback.py`

**Interfaces:**
- Consumes: Task 2's `_start_activity` (with `kind=`), Task 4's catalog (`get()` returns SDK-shaped moves).
- Produces: `MotionManager.play_recorded(kind: str, name: str, move: Any) -> dict` where `kind` is `"emotion"` (priority `EMOTION`) or `"dance"` (priority `DANCE`); result `{"ok": True, "motion": "play_emotion", "emotion": name, "duration_s": <rounded>}` (or the dance equivalents); plus module constant `RECORDED_MOVE_TICK_HZ = 50.0`.

- [ ] **Step 1: Write the failing tests:**

```python
# ABOUTME: Tests for recorded-move playback inside MotionManager — tick loop,
# ABOUTME: clamping, body_yaw pass-through, cancellation, and §18 events.
import threading
import time

import numpy as np
from reachy_mini.utils import create_head_pose

from reachy_openai_realtime.motion import MotionManager, MotionPriority


class FakeRobot:
    def __init__(self) -> None:
        self.targets: list[dict] = []
        self.gotos: list[dict] = []

    def set_target(self, **kwargs) -> None:
        self.targets.append(kwargs)

    def goto_target(self, **kwargs) -> None:
        self.gotos.append(kwargs)

    def get_current_head_pose(self) -> np.ndarray:
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        return ([0.0] * 7, [0.0, 0.0])


class FakeMove:
    """SDK RecordedMove shape: duration + evaluate(t) -> (head, antennas_rad, body_yaw)."""

    def __init__(self, duration: float = 0.2) -> None:
        self.duration = duration
        self.evaluated_at: list[float] = []

    def evaluate(self, t: float):
        if t >= self.duration:
            raise Exception("evaluated beyond duration")  # SDK raises past the last timestamp
        self.evaluated_at.append(t)
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True), np.array([0.1, -0.1]), 0.25


class RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_play_recorded_emotion_ticks_and_completes() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    move = FakeMove(duration=0.2)

    result = manager.play_recorded("emotion", "happy1", move)
    assert result["ok"] is True and result["emotion"] == "happy1"
    assert result["duration_s"] == 0.2
    assert wait_until(lambda: ("emotion.completed" in [e for e, _ in recorder.events]))
    # initial goto to the first frame, then set_target ticks with body_yaw passed through
    assert robot.gotos, "expected an initial goto to the move's start pose"
    assert any(t.get("body_yaw") == 0.25 for t in robot.targets)
    # evaluate() was never called at/beyond duration (clamp works)
    assert all(t < move.duration for t in move.evaluated_at)
    started = [f for e, f in recorder.events if e == "emotion.started"]
    assert started and started[0]["emotion"] == "happy1"
    manager.close()


def test_dance_uses_dance_priority_and_events() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    result = manager.play_recorded("dance", "spin", FakeMove(duration=0.15))
    assert result["ok"] is True and result["dance"] == "spin"
    assert wait_until(lambda: any(e == "dance.completed" for e, _ in recorder.events))
    manager.close()


def test_equal_or_higher_recorded_priority_preempts() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    long_dance = FakeMove(duration=5.0)
    assert manager.play_recorded("dance", "marathon", long_dance)["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)  # dance is running
    replaced = manager.play_recorded("dance", "second", FakeMove())
    assert replaced["ok"] is True  # equal priority (65 >= 65) preempts — arbitration rule 1
    promoted = manager.play_recorded("emotion", "surprise", FakeMove(duration=0.1))
    assert promoted["ok"] is True  # EMOTION 70 >= DANCE 65
    manager.close()


def test_look_is_rejected_during_recorded_move() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    assert manager.play_recorded("emotion", "long", FakeMove(duration=5.0))["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    rejected = manager.submit("look", {"direction": "left"})
    assert rejected["ok"] is False and "busy" in rejected["error"]  # LOOK 45 < EMOTION 70
    manager.close()


def test_stop_cancels_recorded_move_and_emits_motion_cancelled() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    assert manager.play_recorded("dance", "marathon", FakeMove(duration=10.0))["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    manager.submit("stop_motion", {})
    assert wait_until(lambda: ("motion.cancelled", {"motion": "marathon", "reason": "stop"}) in recorder.events)
    assert not any(e == "dance.completed" for e, _ in recorder.events)
    manager.close()


def test_evaluate_exception_ends_move_without_killing_worker() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()

    class BrokenMove(FakeMove):
        def evaluate(self, t: float):
            raise RuntimeError("corrupt trajectory")

    assert manager.play_recorded("emotion", "broken", BrokenMove(duration=1.0))["ok"] is True
    # worker survives: a later gesture still executes
    assert wait_until(lambda: manager.submit("nod", {"count": 1})["ok"] is True)
    assert wait_until(lambda: len(robot.gotos) + len(robot.targets) > 0)
    manager.close()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_motion_playback.py -q`. Expected: `AttributeError: ... play_recorded`.

- [ ] **Step 3: Implement in `manager.py`:**

```python
RECORDED_MOVE_TICK_HZ = 50.0  # own playback loop; bounded WS rate, above the 30 Hz ambient tick


def play_recorded(self, kind: str, name: str, move: Any) -> dict[str, Any]:
    if kind not in {"emotion", "dance"}:
        raise ValueError(f"unknown recorded-move kind: {kind}")
    priority = MotionPriority.EMOTION if kind == "emotion" else MotionPriority.DANCE
    result = self._start_activity(name, priority, lambda: self._run_recorded(move), kind=kind)
    if result.get("ok"):
        result["motion"] = f"play_{kind}"
        result[kind] = name
        result["duration_s"] = round(float(move.duration), 1)
    return result


def _run_recorded(self, move: Any) -> None:
    tick = 1.0 / RECORDED_MOVE_TICK_HZ
    try:
        head, antennas, body_yaw = move.evaluate(0.0)
    except Exception:
        logger.exception("Recorded move failed to evaluate its first frame")
        return
    try:
        self.robot.goto_target(head=head, antennas=antennas, duration=0.4, body_yaw=body_yaw)
    except Exception:
        logger.debug("Initial goto for recorded move failed", exc_info=True)
    started = time.monotonic()
    duration = float(move.duration)
    while not self._cancel_event.is_set() and not self._stop_event.is_set():
        elapsed = time.monotonic() - started
        if elapsed >= duration:
            break
        t = min(elapsed, duration - 1e-2)  # SDK evaluate() raises at/after the last timestamp
        try:
            head, antennas, body_yaw = move.evaluate(t)
        except Exception:
            logger.exception("Recorded move evaluation failed mid-play")
            return
        try:
            self.robot.set_target(head=head, antennas=antennas, body_yaw=body_yaw)
        except Exception:
            logger.debug("Recorded move set_target failed", exc_info=True)
        time.sleep(tick)
```

  Note the worker (Task 2) already emits `<kind>.started`/`<kind>.completed`/`motion.cancelled` around `activity.run()` and gotos back to base afterward — `_run_recorded` only plays frames. `play_recorded` result key: use `result["emotion"] = name` / `result["dance"] = name` per kind (tests pin both).

- [ ] **Step 4: Run the full check** — `uv run ruff check . && uv run pytest`.

- [ ] **Step 5: Commit** — `git commit -am "feat: recorded-move playback through MotionManager tick loop"`.

---

### Task 6: `play_emotion` / `stop_emotion` / `play_dance` / `stop_dance` tools

Wire catalogs into `submit()` and extend the tool definitions. Tool routing stays on the existing `submit` path (ToolExecutor is Phase 3 — issue #21).

**Files:**
- Modify: `reachy_openai_realtime/motion/manager.py` (constructor takes catalogs; submit routing; targeted stops)
- Modify: `reachy_openai_realtime/motion/tools.py` (new defs + `tool_definitions()` helper)
- Modify: `reachy_openai_realtime/motion/__init__.py` (export `tool_definitions`)
- Test: `tests/test_motion_tools.py`

**Interfaces:**
- Consumes: Tasks 4 & 5.
- Produces:
  - `MotionManager.__init__(self, robot, *, emotions: RecordedMoveCatalog | None = None, dances: RecordedMoveCatalog | None = None)` — both default `None` (constructor stays compatible with every existing call site and test)
  - `MotionManager.emotion_names() -> list[str]` / `dance_names() -> list[str]` — `[]` when absent/unavailable
  - `MotionManager.tool_definitions() -> list[dict]` — base `TOOL_DEFINITIONS` + emotion pair when `emotions.available` + dance pair when `dances.available`
  - `motion/tools.py`: `RECORDED_MOVE_TOOL_DEFINITIONS: dict[str, list[dict]]` with keys `"emotion"`, `"dance"`, and `def tool_definitions(*, emotions_available: bool, dances_available: bool) -> list[dict]`

- [ ] **Step 1: Write the failing tests:**

```python
# ABOUTME: Tests for the recorded-move tool surface — submit routing, catalog
# ABOUTME: validation, targeted stops, and availability-gated tool definitions.
import time

import numpy as np
import pytest
from reachy_mini.utils import create_head_pose

from reachy_openai_realtime.motion import (
    EMOTIONS_DATASET,
    MotionManager,
    RecordedMoveCatalog,
    TOOL_DEFINITIONS,
)


class FakeRobot:
    def __init__(self) -> None:
        self.targets: list[dict] = []
        self.gotos: list[dict] = []

    def set_target(self, **kwargs) -> None:
        self.targets.append(kwargs)

    def goto_target(self, **kwargs) -> None:
        self.gotos.append(kwargs)

    def get_current_head_pose(self) -> np.ndarray:
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        return ([0.0] * 7, [0.0, 0.0])


class FakeMove:
    def __init__(self, duration: float = 0.2) -> None:
        self.duration = duration

    def evaluate(self, t: float):
        if t >= self.duration:
            raise Exception("beyond duration")
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True), np.array([0.0, 0.0]), None


class FakeRecordedMoves:
    def __init__(self, names_to_moves: dict) -> None:
        self._moves = names_to_moves

    def list_moves(self) -> list[str]:
        return list(self._moves)

    def get(self, name: str):
        return self._moves[name]


def ready_catalog(names_to_moves: dict) -> RecordedMoveCatalog:
    catalog = RecordedMoveCatalog(EMOTIONS_DATASET, loader=lambda ds: FakeRecordedMoves(names_to_moves))
    catalog.load_async()
    assert catalog.wait_ready(timeout=2.0)
    return catalog


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_play_emotion_routes_through_catalog_and_manager() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove()}))
    manager.start()
    result = manager.submit("play_emotion", {"emotion": "happy1"})
    assert result["ok"] is True and result["emotion"] == "happy1" and result["duration_s"] == 0.2
    manager.close()


def test_play_emotion_unknown_name_raises_value_error() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove()}))
    with pytest.raises(ValueError, match="unknown move"):
        manager.submit("play_emotion", {"emotion": "nonexistent"})


def test_play_emotion_without_catalog_returns_unavailable() -> None:
    manager = MotionManager(FakeRobot())  # no catalogs
    result = manager.submit("play_emotion", {"emotion": "happy1"})
    assert result == {"ok": False, "error": "emotion catalog unavailable"}


def test_play_dance_while_catalog_loading_returns_unavailable() -> None:
    slow = RecordedMoveCatalog(EMOTIONS_DATASET, loader=lambda ds: FakeRecordedMoves({}))
    # never load_async'd -> stays "loading"
    manager = MotionManager(FakeRobot(), dances=slow)
    result = manager.submit("play_dance", {"dance": "spin"})
    assert result["ok"] is False and "unavailable" in result["error"]


def test_stop_emotion_stops_only_active_emotion() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"long": FakeMove(duration=10.0)}))
    manager.start()
    assert manager.submit("play_emotion", {"emotion": "long"})["ok"] is True
    assert wait_until(lambda: manager._current is not None)
    result = manager.submit("stop_emotion", {})
    assert result == {"ok": True, "motion": "stop_emotion", "stopped": True}
    idle_stop = manager.submit("stop_dance", {})
    assert idle_stop == {"ok": True, "motion": "stop_dance", "stopped": False}
    manager.close()


def test_stop_emotion_does_not_clear_background_enables() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"long": FakeMove(duration=10.0)}))
    manager.set_idle_enabled(True)
    manager.start()
    manager.submit("play_emotion", {"emotion": "long"})
    manager.submit("stop_emotion", {})
    assert manager._idle_enabled.is_set()
    manager.close()


def test_tool_definitions_gate_on_catalog_availability() -> None:
    bare = MotionManager(FakeRobot())
    names = [tool["name"] for tool in bare.tool_definitions()]
    assert names == [tool["name"] for tool in TOOL_DEFINITIONS]

    with_emotions = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove()}))
    names = [tool["name"] for tool in with_emotions.tool_definitions()]
    assert "play_emotion" in names and "stop_emotion" in names
    assert "play_dance" not in names


def test_emotion_names_lists_catalog() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove(), "sad2": FakeMove()}))
    assert manager.emotion_names() == ["happy1", "sad2"]
    assert manager.dance_names() == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_motion_tools.py -q`.

- [ ] **Step 3: Implement.**
  - `motion/tools.py` — append (Japanese house style, matching the existing entries):

```python
RECORDED_MOVE_TOOL_DEFINITIONS: dict[str, list[dict[str, Any]]] = {
    "emotion": [
        {
            "type": "function",
            "name": "play_emotion",
            "description": "収録済みの感情ジェスチャーを再生する。emotionには利用可能なエモーション名を正確に指定する。",
            "parameters": {
                "type": "object",
                "properties": {"emotion": {"type": "string"}},
                "required": ["emotion"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "stop_emotion",
            "description": "再生中のエモーションを停止する。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ],
    "dance": [
        {
            "type": "function",
            "name": "play_dance",
            "description": "収録済みのダンスを再生する。danceには利用可能なダンス名を正確に指定する。",
            "parameters": {
                "type": "object",
                "properties": {"dance": {"type": "string"}},
                "required": ["dance"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "stop_dance",
            "description": "再生中のダンスを停止する。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ],
}


def tool_definitions(*, emotions_available: bool, dances_available: bool) -> list[dict[str, Any]]:
    tools = list(TOOL_DEFINITIONS)
    if emotions_available:
        tools.extend(RECORDED_MOVE_TOOL_DEFINITIONS["emotion"])
    if dances_available:
        tools.extend(RECORDED_MOVE_TOOL_DEFINITIONS["dance"])
    return tools
```

  - `manager.py` — constructor grows `emotions`/`dances` keyword-only params stored as `self._emotions`/`self._dances`. Extend `validate()` static shapes: `play_emotion` requires a string `emotion` argument, `play_dance` a string `dance`, `stop_emotion`/`stop_dance` take no arguments (return `MotionCommand(name, {...})`; unknown names still raise). Extend `submit()` routing before the gesture branch:

```python
if name == "play_emotion":
    return self._submit_recorded("emotion", self._emotions, command.arguments["emotion"])
if name == "play_dance":
    return self._submit_recorded("dance", self._dances, command.arguments["dance"])
if name == "stop_emotion":
    return self._stop_recorded("emotion")
if name == "stop_dance":
    return self._stop_recorded("dance")
```

```python
def _submit_recorded(self, kind: str, catalog: RecordedMoveCatalog | None, name: str) -> dict[str, Any]:
    if catalog is None or not catalog.available:
        return {"ok": False, "error": f"{kind} catalog unavailable"}
    move = catalog.get(name)  # ValueError for unknown names propagates to realtime's handler
    return self.play_recorded(kind, name, move)

def _stop_recorded(self, kind: str) -> dict[str, Any]:
    with self._slot_lock:
        active = self._current or self._pending
        matches = active is not None and active.kind == kind
    if matches:
        # cancel the foreground move only; background enables stay untouched
        with self._slot_cv:
            self._pending = None
            self._cancel_reason = "stop"
            self._cancel_event.set()
    return {"ok": True, "motion": f"stop_{kind}", "stopped": matches}

def emotion_names(self) -> list[str]:
    return self._emotions.names() if self._emotions is not None else []

def dance_names(self) -> list[str]:
    return self._dances.names() if self._dances is not None else []

def tool_definitions(self) -> list[dict[str, Any]]:
    return tools.tool_definitions(
        emotions_available=self._emotions is not None and self._emotions.available,
        dances_available=self._dances is not None and self._dances.available,
    )
```

  (Import `tool_definitions` machinery as `from . import tools` or a direct function import — match the file's import style. Note `stop_emotion`/`stop_dance` deliberately do NOT clear background enables — that's `stop_motion`'s job.)

- [ ] **Step 4: Run the full check** — `uv run ruff check . && uv run pytest`.

- [ ] **Step 5: Commit** — `git commit -am "feat: play/stop emotion and dance tools validated against live catalogs"`.

---

### Task 7: Wiring — main.py catalogs, realtime tools + instruction context

Connect everything: catalogs load at startup, the session advertises the tools it can honor, and the model learns the playable names once per session (token economy per §10: names ride session instructions, never per-response instructions).

**Files:**
- Modify: `reachy_openai_realtime/config.py` (new `recorded_moves_instructions`)
- Modify: `reachy_openai_realtime/main.py` (catalog construction ~line 313, warning event on unavailability)
- Modify: `reachy_openai_realtime/realtime.py` (`_session_config()` at :326–342 — instructions + dynamic tools; imports at :40; barge-in reason at :832/:1075)
- Modify: `reachy_openai_realtime/static/i18n.js` (one event-key row)
- Test: `tests/test_realtime_config.py`

**Interfaces:**
- Consumes: `MotionManager.tool_definitions()`, `emotion_names()`, `dance_names()` (Task 6); `session_instructions(language_code)` (config.py:45); `RealtimeRobotSession._session_config()` (realtime.py:326, dict-shaped payload sent via `session.update` at :287).
- Produces: `config.recorded_moves_instructions(emotions: list[str], dances: list[str]) -> str` — returns `""` when both lists are empty, otherwise a `"\n\n"`-prefixed block. No `language_code` param: model-facing instructions are English prose regardless of conversation language (see `session_instructions`, config.py:47–60, and test_realtime_config.py:15), and move names are language-neutral identifiers.

- [ ] **Step 1: Write the failing config tests.** `tests/test_realtime_config.py` is the session-config test file. Add:

```python
from reachy_openai_realtime.config import recorded_moves_instructions


def test_recorded_moves_instructions_empty_when_no_catalogs() -> None:
    assert recorded_moves_instructions([], []) == ""


def test_recorded_moves_instructions_lists_names() -> None:
    text = recorded_moves_instructions(["happy1", "sad2"], ["spin"])
    assert text.startswith("\n\n")
    assert "happy1, sad2" in text and "spin" in text
    assert "play_emotion" in text and "play_dance" in text


def test_recorded_moves_instructions_omits_absent_catalog() -> None:
    text = recorded_moves_instructions(["happy1"], [])
    assert "play_emotion" in text and "happy1" in text
    assert "play_dance" not in text
```

- [ ] **Step 2: Run to verify failure** (`ImportError`), then implement in `config.py`, continuing the bullet style of `session_instructions`' motion-guidance block (config.py:54–59):

```python
def recorded_moves_instructions(emotions: list[str], dances: list[str]) -> str:
    if not emotions and not dances:
        return ""
    lines: list[str] = []
    if emotions:
        lines.append("- play_emotion accepts exactly these names: " + ", ".join(emotions))
    if dances:
        lines.append("- play_dance accepts exactly these names: " + ", ".join(dances))
    return "\n\n" + "\n".join(lines)
```

- [ ] **Step 3: Write the failing session-payload tests.** The realtime change makes `_session_config()` read `self.motion`, so the two EXISTING tests in `tests/test_realtime_config.py` (which build bare sessions via `RealtimeRobotSession.__new__`) must gain a stub; add it and the new test now:

```python
from reachy_openai_realtime.motion import TOOL_DEFINITIONS


class _StubMotion:
    """Minimal MotionManager face for _session_config: bare tools, no catalogs."""

    def tool_definitions(self):
        return list(TOOL_DEFINITIONS)

    def emotion_names(self):
        return []

    def dance_names(self):
        return []


def test_session_config_advertises_catalog_tools_and_names() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()

    class _CatalogMotion(_StubMotion):
        def tool_definitions(self):
            return list(TOOL_DEFINITIONS) + [{"type": "function", "name": "play_emotion"}]

        def emotion_names(self):
            return ["happy1", "sad2"]

    session.motion = _CatalogMotion()
    config = session._session_config()
    assert any(tool.get("name") == "play_emotion" for tool in config["tools"])
    assert "happy1, sad2" in config["instructions"]


def test_session_config_without_catalogs_keeps_base_tools_only() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session.motion = _StubMotion()
    config = session._session_config()
    assert [tool["name"] for tool in config["tools"]] == [tool["name"] for tool in TOOL_DEFINITIONS]
    assert "play_emotion accepts" not in config["instructions"]
```

  Also add `session.motion = _StubMotion()` to the two existing `_session_config` tests in this file (they fail with `AttributeError` otherwise once Step 4 lands). Run: new tests fail (`KeyError`/instructions unchanged), existing ones still pass.

- [ ] **Step 4: Wire `realtime.py`.** Inside `_session_config()` (realtime.py:326–342):

```python
instructions=session_instructions(self._current_language())
+ recorded_moves_instructions(self.motion.emotion_names(), self.motion.dance_names()),
...
tools=self.motion.tool_definitions(),  # type: ignore[arg-type]
```

  Remove the now-unused `TOOL_DEFINITIONS` import from realtime.py; import `recorded_moves_instructions` alongside the other config imports (realtime.py:28–31). Availability is a per-connection snapshot: sessions reconnect at least hourly (60-min server cap), so a catalog that finishes loading after connect is picked up on the next session (documented limitation — do not add a mid-session `session.update` refresh; robustness > features). At the interruption call sites realtime.py:832 and :1075, pass the reason through: `self.motion.stop_current(reason="barge_in")` (the reset path at :1164 keeps the default `"stop"`). Run the Step 3 tests: all green.

- [ ] **Step 5: Wire `main.py`** (around line 313):

```python
emotions_catalog = RecordedMoveCatalog(EMOTIONS_DATASET)
dances_catalog = RecordedMoveCatalog(DANCES_DATASET)
emotions_catalog.load_async()
dances_catalog.load_async()
motion = MotionManager(reachy_mini, emotions=emotions_catalog, dances=dances_catalog)
```

  After the audio-tuning block (where the app already emits startup events), add a non-blocking availability note — wait briefly, warn only, never block the conversation (§25):

```python
for catalog in (emotions_catalog, dances_catalog):
    if not catalog.wait_ready(timeout=5.0):
        self.runtime_status.add_event(
            f"収録モーションのカタログを読み込めていません: {catalog.dataset}",
            "warning",
            key="event_motion_catalog_unavailable",
            params={"dataset": catalog.dataset},
        )
```

  (Verified signature: `add_event(message, level="info", *, key=None, params=None)` — runtime_status.py:331. 5 s covers warm-cache loads on the robot — the daemon preloads both datasets; a cold dev box just logs the warning and the catalog may still become ready later, with tools appearing on the next reconnect.) Add the matching row to the `rows` table in `reachy_openai_realtime/static/i18n.js`, locale order per the `LOCALES` list at the top of that file (en, ja, zh, ko, es, fr, de, it, pt):

```js
event_motion_catalog_unavailable: ["Could not load the recorded-move catalog: {dataset}", "収録モーションのカタログを読み込めていません: {dataset}", "无法加载录制动作库：{dataset}", "녹화된 모션 카탈로그를 불러오지 못했습니다: {dataset}", "No se pudo cargar el catálogo de movimientos grabados: {dataset}", "Impossible de charger le catalogue de mouvements enregistrés : {dataset}", "Katalog aufgezeichneter Bewegungen konnte nicht geladen werden: {dataset}", "Impossibile caricare il catalogo dei movimenti registrati: {dataset}", "Não foi possível carregar o catálogo de movimentos gravados: {dataset}"],
```

- [ ] **Step 6: Run the full check** — `uv run ruff check . && uv run pytest`.

- [ ] **Step 7: Commit** — `git commit -am "feat: wire recorded-move catalogs into session tools and instructions"`.

---

### Task 8: Documentation + branch finish

**Files:**
- Modify: `README.md` (tool list / motion section), `gotchas.md`

**Interfaces:** none (prose only).

- [ ] **Step 1: README.** Find the existing tool documentation (`grep -n "stop_motion\|nod" README.md`) and add `play_emotion`/`stop_emotion`/`play_dance`/`stop_dance` with one line each: validated against the live Hugging Face catalogs (`pollen-robotics/reachy-mini-emotions-library`, `-dances-library`); when a catalog is unavailable the tools are absent from the session and voice conversation continues (§25). Document the priority table briefly (copy the §12 values) and that `stop_motion` always wins.

- [ ] **Step 2: gotchas.md.** Append one bullet:

```markdown
- **Recorded moves are played by our own MotionManager loop, never `ReachyMini.play_move`.**
  `play_move`'s cancel path is `cancel_move()` → `media.stop_playing()`, which stalls the shared
  Wireless mic pipeline. Sidecar emotion sounds are skipped for the same reason (the speaker
  belongs to the Realtime audio path). Catalog names are sanitized (`^[A-Za-z0-9 _-]{1,64}$`)
  before they enter session instructions — dataset filenames are third-party input.
```

- [ ] **Step 3: Full check** — `uv run ruff check . && uv run pytest`.

- [ ] **Step 4: Final commit** with the issue footers so the branch closes its trackers on merge:

```bash
git commit -am "docs: recorded-move tools, priority table, and motion gotchas

Closes #13
Closes #14
Closes #15"
```

---

## Notes and deferred decisions

- **Sidecar sounds are OFF in Phase 2.** Emotion/dance datasets ship audio files; the SDK plays them via `media_manager.play_sound`, the same pipeline the Realtime speaker owns. Mixing decisions (duck the conversation? queue behind assistant audio?) are a feature discussion — filed thought for Phase 4/UI review. The plan's playback loop simply never touches media.
- **`express` stays in Phase 2** (preserve current behavior, §31). Spec §13's Phase 3 tool surface omits it; retiring it is ToolExecutor work (#21).
- **`look_at_speaker` (priority 50) is Phase 3** — needs DoA (#9). The `MotionPriority` docstring reserves the value.
- **No mid-session tool refresh** when a catalog becomes ready after connect: next reconnect (≤60 min away by server cap) picks it up. Revisit only if it bites in practice.
- **Behavior change vs today, intentional:** tool gestures no longer clear the ambient enable flags (they suppress-and-resume instead, per §12), and the 8-deep FIFO queue is gone (same-or-higher priority preempts, lower is rejected with a "busy" error the model can react to). Barge-in (`stop_current`) still silences everything, now with `reason="barge_in"` in the `motion.cancelled` event.
- **§30 Motion acceptance mapping:** "all movement passes through MotionManager" (Tasks 1–2, 5 — ambient + gestures + recorded moves share the one worker), "HF emotions/dances dynamically discoverable and playable" (Tasks 4–7), "background motion does not fight recorded moves" (Task 2 rule 2), "stop always preempts" (Task 2 rule 3, tested in Tasks 2 and 5).

## Estimated scale

~650 lines of implementation change (mostly `manager.py`) + ~600 lines of new tests across 8 tasks / ~12 commits.
