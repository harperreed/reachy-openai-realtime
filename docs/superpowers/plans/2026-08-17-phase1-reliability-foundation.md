# Phase 1 — Reliability Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the existing Reachy ↔ OpenAI Realtime session an explicit FSM, connection epochs, watchdogs, jittered reconnect, dedicated audio workers, a latency-bounded playback buffer, and a structured event recorder — so the app self-heals from network/audio faults without manual restarts.

**Architecture:** Evolve `reachy_openai_realtime/realtime.py` in place (no rewrite). New pure-logic components live in three new subpackages — `session/` (fsm, watchdog, recovery), `audio/` (capture, playback workers), `observability/` (events, metrics) — each unit-testable without a robot or network. `realtime.py` keeps orchestration and integrates them incrementally, task by task, with existing tests updated as booleans give way to the FSM.

**Tech Stack:** Python ≥3.10, asyncio + threading primitives only (no new runtime deps), `openai` SDK Realtime WebSocket, numpy, pytest + ruff.

**Spec:** `docs/production-hardening-spec.md` — this plan implements Phase 1 (§29), covering spec §2–§9, §18, §19, and the Phase-1-relevant rows of §26–§28. Phases 2–6 get their own plan docs later.

## Global Constraints

- Python floor is **3.10**: no `asyncio.timeout()`, no `enum.StrEnum` (spec-repo `pyproject.toml`).
- Ruff line length **110**; canonical check is `uv run ruff check .` && `uv run pytest`.
- Evolve, don't rewrite; no giant refactor commit — each task commits separately (spec §31).
- No Hermes/OpenClaw/LangChain/second LLM; OpenAI Realtime stays the sole conversational path (spec §1, §31).
- Never store or return the OpenAI API key; logs must contain no keys or raw mic audio (spec §18, §31). Redaction is mandatory in the event recorder.
- Never reboot Reachy automatically (spec §6, §24).
- Ambiguity resolution order: robustness > conversational latency > motion safety > simplicity > completeness (spec §31).
- **Reachy Mini Wireless shares ONE GStreamer pipeline for capture and playback.** Never call `media.stop_playing()` or `ReachyMini.cancel_move()` from audio/interrupt paths (it stalls the mic); after `audio.clear_player()` always re-assert `media.start_recording()`. This constraint is load-bearing in `realtime.py:_clear_playback` and `motion.py:stop_current` — preserve it in every new audio path.
- Optional-dependency degradation: nothing in Phase 1 may make the app fail to import when robot hardware is absent (tests run on dev machines).

## Research notes (verified 2026-08-17, OpenAI docs)

- `gpt-realtime-2.1` is current; bare `gpt-realtime`/`gpt-realtime-mini` are deprecated (shutdown 2027-01-20). Voices include `marin`, `cedar`. `session.reasoning.effort` accepts `minimal|low|medium|high|xhigh`.
- **Realtime sessions hard-cap at 60 minutes.** The server WILL close healthy long conversations — reconnect is routine operation, not just failure handling. Treat server-initiated close as `TRANSIENT`.
- `response.cancelled` is a distinct terminal server event; the cancel watchdog must accept `response.done` OR `response.cancelled` as terminal.
- `error` server events carry `event_id` correlating to the client event that caused them (the camera error path already uses this).

Reachy Mini SDK (verified 2026-08-17, pollen-robotics/reachy_mini source + issue tracker):

- `media.get_audio_sample()` returns **all samples buffered since the last call** (float32, shape `(N, 2)`, 16 kHz) and clears the buffer; `None` during init. Issue #436: the buffer grows unbounded (to OOM) if never drained — the capture worker must drain continuously regardless of session state, gating downstream.
- Known open issues: mic returns **all-zero frames** after suspend/idle (#738, #770) while frames keep arriving — the spec §6 stall watchdog (frame *absence*) won't see this; VAD won't false-trigger on zeros either. Noted for Phase 6 soak; not built in Phase 1.
- `ReachyMini.cancel_move()` calls `media_manager.stop_playing()` — confirmed; never call it from audio/interrupt paths (see Global Constraints).
- The dashboard daemon does **not** auto-restart crashed apps (traceback stored, process ends). "Restart app session" (mic-ladder attempt 3) means our own outer loop in `main.py:run()`, which is why it stays.
- App stop is SIGINT with a 20 s SIGKILL deadline — worker `close()` methods must use bounded `join(timeout=...)`.

---

### Task 1: Structured event recorder (`observability/events.py`)

**Files:**
- Create: `reachy_openai_realtime/observability/__init__.py` (empty package marker)
- Create: `reachy_openai_realtime/observability/events.py`
- Test: `tests/test_observability_events.py`

**Interfaces:**
- Consumes: nothing (pure new code).
- Produces (later tasks import these exact names from `reachy_openai_realtime.observability.events`):
  - `redact_secrets(text: str) -> str`
  - `class EventRecorder:`
    - `__init__(self, path: Path, *, max_bytes: int = 5_000_000, keep_files: int = 2) -> None`
    - `set_context_providers(self, *, epoch: Callable[[], int] | None = None, state: Callable[[], str] | None = None) -> None`
    - `record(self, event: str, **fields: Any) -> None` — thread-safe, never raises
    - `close(self) -> None` — idempotent

- [ ] **Step 1: Check package discovery.** Read `pyproject.toml`. If it declares an explicit package list (e.g. `[tool.setuptools] packages = [...]`), add `reachy_openai_realtime.observability` (and note that Tasks 4/9 add `session`/`audio`). If it uses auto-discovery (no explicit list, or `packages.find`), no change needed — subpackages with `__init__.py` are found automatically.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_observability_events.py
import json
import threading

from reachy_openai_realtime.observability.events import EventRecorder, redact_secrets


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_redact_secrets_masks_api_keys() -> None:
    assert redact_secrets("key sk-proj-abcdef1234567890 leaked") == "key sk-*** leaked"
    assert redact_secrets("no secrets here") == "no secrets here"


def test_record_writes_enriched_jsonl(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    recorder.set_context_providers(epoch=lambda: 7, state=lambda: "LISTENING")
    recorder.record("realtime.connected", model="gpt-realtime-2.1")
    recorder.close()

    (entry,) = read_lines(tmp_path / "events.jsonl")
    assert entry["event"] == "realtime.connected"
    assert entry["connection_epoch"] == 7
    assert entry["session_state"] == "LISTENING"
    assert entry["model"] == "gpt-realtime-2.1"
    assert entry["timestamp"].endswith("+00:00")


def test_record_without_providers_omits_context_fields(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    recorder.record("app.start")
    recorder.close()
    (entry,) = read_lines(tmp_path / "events.jsonl")
    assert "connection_epoch" not in entry
    assert "session_state" not in entry


def test_record_redacts_nested_field_values(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    recorder.record(
        "realtime.error",
        message="auth failed for sk-proj-abcdef1234567890",
        detail={"headers": ["Bearer sk-proj-abcdef1234567890"]},
    )
    recorder.close()
    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "sk-proj-abcdef1234567890" not in raw
    assert "sk-***" in raw


def test_rotation_keeps_bounded_files(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path, max_bytes=500, keep_files=2)
    for index in range(60):
        recorder.record("fsm.transition", index=index, padding="x" * 40)
    recorder.close()

    assert path.exists()
    assert (tmp_path / "events.jsonl.1").exists()
    assert not (tmp_path / "events.jsonl.3").exists()
    # newest file stays small after rotation
    assert path.stat().st_size < 5_000


def test_record_survives_unwritable_directory(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "missing" / "deep" / "events.jsonl")
    recorder.record("app.start")  # must not raise even though parent dirs vanish
    recorder.close()


def test_record_is_thread_safe(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path)
    threads = [
        threading.Thread(target=lambda: [recorder.record("tick") for _ in range(50)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    recorder.close()
    assert len(read_lines(path)) == 200


def test_provider_exception_does_not_break_recording(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")

    def broken_epoch() -> int:
        raise RuntimeError("not connected yet")

    recorder.set_context_providers(epoch=broken_epoch, state=lambda: "DISCONNECTED")
    recorder.record("app.start")
    recorder.close()
    (entry,) = read_lines(tmp_path / "events.jsonl")
    assert "connection_epoch" not in entry
    assert entry["session_state"] == "DISCONNECTED"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_observability_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reachy_openai_realtime.observability'`

- [ ] **Step 4: Implement**

Create empty `reachy_openai_realtime/observability/__init__.py`, then:

```python
# reachy_openai_realtime/observability/events.py
# ABOUTME: JSONL flight recorder for runtime events, with secret redaction and
# ABOUTME: size-based rotation. Canonical home of log-redaction helpers.
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

logger = logging.getLogger(__name__)

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


def redact_secrets(text: str) -> str:
    """Mask OpenAI-style API keys anywhere in a string."""
    return _SECRET_PATTERN.sub("sk-***", text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


class EventRecorder:
    """Append-only JSONL event log. Thread-safe; recording never raises."""

    def __init__(self, path: Path, *, max_bytes: int = 5_000_000, keep_files: int = 2) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._keep_files = keep_files
        self._lock = threading.Lock()
        self._file: TextIO | None = None
        self._epoch_provider: Callable[[], int] | None = None
        self._state_provider: Callable[[], str] | None = None

    def set_context_providers(
        self,
        *,
        epoch: Callable[[], int] | None = None,
        state: Callable[[], str] | None = None,
    ) -> None:
        self._epoch_provider = epoch
        self._state_provider = state

    def record(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        for key, provider in (
            ("connection_epoch", self._epoch_provider),
            ("session_state", self._state_provider),
        ):
            if provider is None:
                continue
            try:
                entry[key] = provider()
            except Exception:
                # Context is best-effort; a half-initialized session must not
                # stop the flight recorder.
                continue
        entry.update(_redact_value(fields))
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._write_locked(line)
            except OSError as exc:
                logger.debug("event recorder write failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None

    def _write_locked(self, line: str) -> None:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")
        self._file.write(line + "\n")
        self._file.flush()
        if self._file.tell() >= self._max_bytes:
            self._rotate_locked()

    def _rotate_locked(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        for index in range(self._keep_files - 1, 0, -1):
            source = self._path.with_name(f"{self._path.name}.{index}")
            if source.exists():
                source.replace(self._path.with_name(f"{self._path.name}.{index + 1}"))
        overflow = self._path.with_name(f"{self._path.name}.{self._keep_files + 1}")
        if overflow.exists():
            overflow.unlink()
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_observability_events.py -v`
Expected: all PASS

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check .
git add reachy_openai_realtime/observability tests/test_observability_events.py pyproject.toml
git commit -m "feat: add JSONL event recorder with redaction and rotation"
```

---

### Task 2: Metrics registry (`observability/metrics.py`)

**Files:**
- Create: `reachy_openai_realtime/observability/metrics.py`
- Test: `tests/test_observability_metrics.py`

**Interfaces:**
- Consumes: nothing.
- Produces (imported later from `reachy_openai_realtime.observability.metrics`):
  - `class LatencyStat: __init__(self, window: int = 200); record(self, value_ms: float) -> None; snapshot(self) -> dict[str, float | int]` — snapshot keys exactly `count,min,max,mean,p50,p95`; `count` is lifetime, the rest cover the recent window.
  - `class MetricsRegistry: observe_ms(self, name: str, value_ms: float) -> None; increment(self, name: str, amount: int = 1) -> None; set_gauge(self, name: str, value: float) -> None; snapshot(self) -> dict[str, Any]` — snapshot shape `{"latency": {name: stat_dict}, "counters": {name: int}, "gauges": {name: float}}`. All methods thread-safe.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_observability_metrics.py
import threading

from reachy_openai_realtime.observability.metrics import LatencyStat, MetricsRegistry


def test_latency_stat_aggregates() -> None:
    stat = LatencyStat()
    for value in [100.0, 200.0, 300.0, 400.0]:
        stat.record(value)
    snapshot = stat.snapshot()
    assert snapshot["count"] == 4
    assert snapshot["min"] == 100.0
    assert snapshot["max"] == 400.0
    assert snapshot["mean"] == 250.0
    assert 200.0 <= snapshot["p50"] <= 300.0
    assert snapshot["p95"] == 400.0


def test_latency_stat_empty_snapshot_is_zeroed() -> None:
    snapshot = LatencyStat().snapshot()
    assert snapshot == {"count": 0, "min": 0.0, "max": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0}


def test_latency_stat_window_bounds_memory_but_count_is_lifetime() -> None:
    stat = LatencyStat(window=10)
    for value in range(100):
        stat.record(float(value))
    snapshot = stat.snapshot()
    assert snapshot["count"] == 100
    assert snapshot["min"] == 90.0  # only the recent window remains


def test_registry_snapshot_shape() -> None:
    registry = MetricsRegistry()
    registry.observe_ms("speech_end_to_first_audio_played_ms", 640.0)
    registry.increment("reconnect_count")
    registry.increment("reconnect_count", 2)
    registry.set_gauge("queued_audio_ms", 180.0)
    snapshot = registry.snapshot()
    assert snapshot["latency"]["speech_end_to_first_audio_played_ms"]["count"] == 1
    assert snapshot["counters"]["reconnect_count"] == 3
    assert snapshot["gauges"]["queued_audio_ms"] == 180.0


def test_registry_is_thread_safe() -> None:
    registry = MetricsRegistry()
    threads = [
        threading.Thread(target=lambda: [registry.increment("ticks") for _ in range(1000)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert registry.snapshot()["counters"]["ticks"] == 4000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_observability_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# reachy_openai_realtime/observability/metrics.py
# ABOUTME: In-process latency/counter/gauge metrics with bounded windows,
# ABOUTME: exposed through RuntimeStatus.snapshot() into /api/diagnostics.
from __future__ import annotations

import threading
from collections import deque
from typing import Any


def _percentile(data: list[float], percent: float) -> float:
    index = round((len(data) - 1) * percent / 100.0)
    return data[index]


class LatencyStat:
    """Rolling latency aggregate: lifetime count, window-bounded percentiles."""

    def __init__(self, window: int = 200) -> None:
        self._values: deque[float] = deque(maxlen=window)
        self._count = 0

    def record(self, value_ms: float) -> None:
        self._count += 1
        self._values.append(float(value_ms))

    def snapshot(self) -> dict[str, float | int]:
        if not self._values:
            return {"count": self._count, "min": 0.0, "max": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0}
        data = sorted(self._values)
        return {
            "count": self._count,
            "min": data[0],
            "max": data[-1],
            "mean": sum(data) / len(data),
            "p50": _percentile(data, 50.0),
            "p95": _percentile(data, 95.0),
        }


class MetricsRegistry:
    """Thread-safe named metrics: latency observations, counters, gauges."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latency: dict[str, LatencyStat] = {}
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}

    def observe_ms(self, name: str, value_ms: float) -> None:
        with self._lock:
            stat = self._latency.get(name)
            if stat is None:
                stat = self._latency[name] = LatencyStat()
            stat.record(value_ms)

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "latency": {name: stat.snapshot() for name, stat in self._latency.items()},
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_observability_metrics.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add reachy_openai_realtime/observability/metrics.py tests/test_observability_metrics.py
git commit -m "feat: add metrics registry with latency percentiles"
```

---

### Task 3: Wire recorder + metrics into RuntimeStatus, settings paths, and main

**Files:**
- Modify: `reachy_openai_realtime/settings.py` (add `events_path()`, `log_path()` next to `usage_path()`)
- Modify: `reachy_openai_realtime/runtime_status.py` (attach recorder + metrics; delegate redaction to observability)
- Modify: `reachy_openai_realtime/main.py` (construct recorder, add rotating `application.log` handler, record `app.start`/`app.stop`)
- Test: `tests/test_runtime_status.py` (extend), `tests/test_settings.py` (extend)

**Interfaces:**
- Consumes: `EventRecorder`, `redact_secrets` (Task 1); `MetricsRegistry` (Task 2).
- Produces:
  - `settings.events_path() -> Path` = `config_dir() / "events.jsonl"`; `settings.log_path() -> Path` = `config_dir() / "application.log"`
  - `RuntimeStatus.attach_recorder(recorder: EventRecorder) -> None`
  - `RuntimeStatus.metrics: MetricsRegistry` (constructed in `RuntimeStatus.__init__`; single instance per app)
  - `RuntimeStatus.snapshot()` gains a `"metrics"` key with `MetricsRegistry.snapshot()` output.
  - `RuntimeStatus.add_event(...)`, `set_phase(...)`, and `record_error(...)` mirror into the recorder as events `status.message`, `status.phase`, `status.error` when a recorder is attached. Explicit taxonomy events (`fsm.transition`, `realtime.*`, …) are recorded at their source sites in later tasks, not synthesized here.
- `RuntimeStatus.safe_message` keeps its existing name/signature but now delegates to `redact_secrets`; delete the duplicated `_SECRET_PATTERN` from `runtime_status.py` (one source of truth).

- [ ] **Step 1: Write the failing tests.** Add to `tests/test_settings.py`:

```python
def test_events_and_log_paths_live_in_config_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path))
    from reachy_openai_realtime import settings

    assert settings.events_path() == tmp_path / "events.jsonl"
    assert settings.log_path() == tmp_path / "application.log"
```

Add to `tests/test_runtime_status.py`:

```python
def test_add_event_mirrors_into_recorder(tmp_path) -> None:
    import json

    from reachy_openai_realtime.observability.events import EventRecorder
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    recorder = EventRecorder(tmp_path / "events.jsonl")
    status = RuntimeStatus()
    status.attach_recorder(recorder)
    status.add_event("info", "connection ready sk-proj-abcdef1234567890")
    recorder.close()

    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    mirrored = [entry for entry in lines if entry["event"] == "status.message"]
    assert mirrored
    assert "sk-***" in mirrored[0]["message"]
    assert "sk-proj-abcdef1234567890" not in json.dumps(lines)


def test_snapshot_includes_metrics() -> None:
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    status = RuntimeStatus()
    status.metrics.increment("reconnect_count")
    snapshot = status.snapshot()
    assert snapshot["metrics"]["counters"]["reconnect_count"] == 1


def test_status_without_recorder_still_works() -> None:
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    status = RuntimeStatus()
    status.add_event("info", "no recorder attached")  # must not raise
    assert status.snapshot()["metrics"]["counters"] == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runtime_status.py tests/test_settings.py -v`
Expected: new tests FAIL (`AttributeError: events_path` / `attach_recorder` / no `metrics` key)

- [ ] **Step 3: Implement settings paths.** In `settings.py`, next to `usage_path()`:

```python
def events_path() -> Path:
    return config_dir() / "events.jsonl"


def log_path() -> Path:
    return config_dir() / "application.log"
```

- [ ] **Step 4: Implement RuntimeStatus wiring.** In `runtime_status.py`:
  - Replace the local `_SECRET_PATTERN` + body of `safe_message` with `from .observability.events import redact_secrets` and delegate (keep the method so existing call sites don't change).
  - In `__init__`: `self.metrics = MetricsRegistry()` and `self._recorder: EventRecorder | None = None` (import `MetricsRegistry` from `.observability.metrics`).
  - `def attach_recorder(self, recorder): self._recorder = recorder`
  - Add a public helper (later tasks call it from `realtime.py` for taxonomy events like `fsm.transition` and `watchdog.triggered`) and call it from the three mirror points:

```python
def record_event(self, event: str, **fields: object) -> None:
    """Forward a structured event to the flight recorder, if one is attached."""
    if self._recorder is not None:
        self._recorder.record(event, **fields)
```

  - `add_event(...)` → after appending to `_events`: `self.record_event("status.message", level=level, message=<the safe message>, key=detail_key)` (match the actual local variable names in that method when editing).
  - `set_phase(...)` → `self.record_event("status.phase", phase=phase, connected=connected)`.
  - `record_error(...)` → `self.record_event("status.error", message=<safe message>)`.
  - In `snapshot()`, add `"metrics": self.metrics.snapshot()` to the returned dict.

- [ ] **Step 5: Implement main.py wiring.** In `ReachyOpenaiRealtime.run()` (before the API-key wait loop):

```python
from logging.handlers import RotatingFileHandler

from .observability.events import EventRecorder
from .settings import events_path, log_path

recorder = EventRecorder(events_path())
self.status.attach_recorder(recorder)
file_handler = RotatingFileHandler(log_path(), maxBytes=2_000_000, backupCount=2)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().addHandler(file_handler)
recorder.record("app.start")
```

and in the shutdown path (the `finally` around the run loop):

```python
recorder.record("app.stop")
recorder.close()
logging.getLogger().removeHandler(file_handler)
```

Store the recorder on `self` (`self._recorder = recorder`) so the session-construction site in the run loop can pass it onward in later tasks. Keep exact insertion points consistent with the existing structure of `run()` — the recorder must exist before the first `status.add_event` call you want mirrored.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS (existing RuntimeStatus tests unaffected — recorder is optional)

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check .
git add reachy_openai_realtime/settings.py reachy_openai_realtime/runtime_status.py reachy_openai_realtime/main.py tests/test_runtime_status.py tests/test_settings.py
git commit -m "feat: wire event recorder and metrics into runtime status and app lifecycle"
```

---

### Task 4: Session state machine (`session/fsm.py`)

**Files:**
- Create: `reachy_openai_realtime/session/__init__.py` (empty package marker)
- Create: `reachy_openai_realtime/session/fsm.py`
- Test: `tests/test_session_fsm.py`

**Interfaces:**
- Consumes: nothing.
- Produces (imported later from `reachy_openai_realtime.session.fsm`):
  - `class SessionState(Enum)` with members exactly: `DISCONNECTED, CONNECTING, INITIALIZING, LISTENING, USER_SPEAKING, WAITING_RESPONSE, ASSISTANT_SPEAKING, INTERRUPTING, TOOL_EXECUTION, RECOVERING, STOPPING`
  - `LEGAL_TRANSITIONS: dict[SessionState, frozenset[SessionState]]`
  - `class SessionStateMachine:`
    - `__init__(self, *, on_transition: Callable[[SessionState, SessionState, str], None] | None = None, strict: bool = False) -> None` — starts in `DISCONNECTED`
    - `state: SessionState` (property)
    - `transition(self, new_state: SessionState, *, reason: str) -> bool` — same-state is a silent no-op returning True; illegal logs a warning and returns False (raises `AssertionError` when `strict`); listener fires only on real changes.
    - `accepts_user_audio(self) -> bool` — True in `LISTENING, USER_SPEAKING, ASSISTANT_SPEAKING, INTERRUPTING`
    - `generation_active(self) -> bool` — True in `WAITING_RESPONSE, TOOL_EXECUTION, ASSISTANT_SPEAKING`

**Transition table** (spec §3 examples, completed for the real event flow; every state may also enter `RECOVERING` and `STOPPING` except as noted):

```text
DISCONNECTED       → CONNECTING
CONNECTING         → INITIALIZING
INITIALIZING       → LISTENING
LISTENING          → USER_SPEAKING | WAITING_RESPONSE   (greeting/tool responses start without user speech)
USER_SPEAKING      → WAITING_RESPONSE | LISTENING       (turn abandoned)
WAITING_RESPONSE   → ASSISTANT_SPEAKING | TOOL_EXECUTION | LISTENING   (empty/cancelled response)
ASSISTANT_SPEAKING → INTERRUPTING | LISTENING | TOOL_EXECUTION         (tool call mid-audio)
INTERRUPTING       → USER_SPEAKING | LISTENING
TOOL_EXECUTION     → WAITING_RESPONSE | LISTENING
RECOVERING         → CONNECTING | DISCONNECTED | STOPPING   (no RECOVERING→RECOVERING listener spam; same-state no-op covers it)
STOPPING           → DISCONNECTED                            (STOPPING may not re-enter RECOVERING)
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_session_fsm.py
import pytest

from reachy_openai_realtime.session.fsm import LEGAL_TRANSITIONS, SessionState, SessionStateMachine


def test_starts_disconnected() -> None:
    assert SessionStateMachine().state is SessionState.DISCONNECTED


def test_happy_path_conversation_cycle() -> None:
    transitions: list[tuple[SessionState, SessionState, str]] = []
    fsm = SessionStateMachine(on_transition=lambda old, new, reason: transitions.append((old, new, reason)))
    path = [
        (SessionState.CONNECTING, "socket_opening"),
        (SessionState.INITIALIZING, "socket_open"),
        (SessionState.LISTENING, "session_updated"),
        (SessionState.USER_SPEAKING, "vad_started"),
        (SessionState.WAITING_RESPONSE, "turn_committed"),
        (SessionState.ASSISTANT_SPEAKING, "first_audio_received"),
        (SessionState.LISTENING, "playback_finished"),
    ]
    for state, reason in path:
        assert fsm.transition(state, reason=reason) is True
    assert fsm.state is SessionState.LISTENING
    assert [entry[2] for entry in transitions] == [reason for _, reason in path]


def test_barge_in_path() -> None:
    fsm = SessionStateMachine()
    for state in [
        SessionState.CONNECTING,
        SessionState.INITIALIZING,
        SessionState.LISTENING,
        SessionState.WAITING_RESPONSE,
        SessionState.ASSISTANT_SPEAKING,
    ]:
        assert fsm.transition(state, reason="setup") is True
    assert fsm.transition(SessionState.INTERRUPTING, reason="barge_in") is True
    assert fsm.transition(SessionState.USER_SPEAKING, reason="vad_started") is True


def test_illegal_transition_returns_false_and_keeps_state() -> None:
    fsm = SessionStateMachine()
    assert fsm.transition(SessionState.ASSISTANT_SPEAKING, reason="nope") is False
    assert fsm.state is SessionState.DISCONNECTED


def test_illegal_transition_raises_in_strict_mode() -> None:
    fsm = SessionStateMachine(strict=True)
    with pytest.raises(AssertionError):
        fsm.transition(SessionState.ASSISTANT_SPEAKING, reason="nope")


def test_same_state_is_noop_without_listener_call() -> None:
    calls: list[str] = []
    fsm = SessionStateMachine(on_transition=lambda old, new, reason: calls.append(reason))
    fsm.transition(SessionState.CONNECTING, reason="first")
    assert fsm.transition(SessionState.CONNECTING, reason="again") is True
    assert calls == ["first"]


def test_any_active_state_may_recover_but_stopping_may_not() -> None:
    for state, allowed in LEGAL_TRANSITIONS.items():
        if state in (SessionState.STOPPING, SessionState.RECOVERING):
            continue
        assert SessionState.RECOVERING in allowed, state
        assert SessionState.STOPPING in allowed, state
    assert SessionState.RECOVERING not in LEGAL_TRANSITIONS[SessionState.STOPPING]
    assert LEGAL_TRANSITIONS[SessionState.STOPPING] == frozenset({SessionState.DISCONNECTED})


def test_query_helpers() -> None:
    fsm = SessionStateMachine()
    assert fsm.accepts_user_audio() is False
    for state in [SessionState.CONNECTING, SessionState.INITIALIZING, SessionState.LISTENING]:
        fsm.transition(state, reason="setup")
    assert fsm.accepts_user_audio() is True
    assert fsm.generation_active() is False
    fsm.transition(SessionState.WAITING_RESPONSE, reason="setup")
    assert fsm.generation_active() is True


def test_listener_exception_does_not_block_transition() -> None:
    def broken(old: SessionState, new: SessionState, reason: str) -> None:
        raise RuntimeError("listener bug")

    fsm = SessionStateMachine(on_transition=broken)
    assert fsm.transition(SessionState.CONNECTING, reason="setup") is True
    assert fsm.state is SessionState.CONNECTING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session_fsm.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

Create empty `reachy_openai_realtime/session/__init__.py`, then:

```python
# reachy_openai_realtime/session/fsm.py
# ABOUTME: Explicit session state machine (spec §3). One transition() entry
# ABOUTME: point; illegal transitions warn (or assert in strict/test mode).
from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Callable

logger = logging.getLogger(__name__)


class SessionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    INITIALIZING = auto()
    LISTENING = auto()
    USER_SPEAKING = auto()
    WAITING_RESPONSE = auto()
    ASSISTANT_SPEAKING = auto()
    INTERRUPTING = auto()
    TOOL_EXECUTION = auto()
    RECOVERING = auto()
    STOPPING = auto()


_ALWAYS = frozenset({SessionState.RECOVERING, SessionState.STOPPING})

LEGAL_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.DISCONNECTED: frozenset({SessionState.CONNECTING}) | _ALWAYS,
    SessionState.CONNECTING: frozenset({SessionState.INITIALIZING}) | _ALWAYS,
    SessionState.INITIALIZING: frozenset({SessionState.LISTENING}) | _ALWAYS,
    SessionState.LISTENING: frozenset({SessionState.USER_SPEAKING, SessionState.WAITING_RESPONSE}) | _ALWAYS,
    SessionState.USER_SPEAKING: frozenset({SessionState.WAITING_RESPONSE, SessionState.LISTENING}) | _ALWAYS,
    SessionState.WAITING_RESPONSE: frozenset(
        {SessionState.ASSISTANT_SPEAKING, SessionState.TOOL_EXECUTION, SessionState.LISTENING}
    )
    | _ALWAYS,
    SessionState.ASSISTANT_SPEAKING: frozenset(
        {SessionState.INTERRUPTING, SessionState.LISTENING, SessionState.TOOL_EXECUTION}
    )
    | _ALWAYS,
    SessionState.INTERRUPTING: frozenset({SessionState.USER_SPEAKING, SessionState.LISTENING}) | _ALWAYS,
    SessionState.TOOL_EXECUTION: frozenset({SessionState.WAITING_RESPONSE, SessionState.LISTENING}) | _ALWAYS,
    SessionState.RECOVERING: frozenset(
        {SessionState.CONNECTING, SessionState.DISCONNECTED, SessionState.STOPPING}
    ),
    SessionState.STOPPING: frozenset({SessionState.DISCONNECTED}),
}

_ACCEPTS_USER_AUDIO = frozenset(
    {
        SessionState.LISTENING,
        SessionState.USER_SPEAKING,
        SessionState.ASSISTANT_SPEAKING,
        SessionState.INTERRUPTING,
    }
)

_GENERATION_ACTIVE = frozenset(
    {SessionState.WAITING_RESPONSE, SessionState.TOOL_EXECUTION, SessionState.ASSISTANT_SPEAKING}
)


class SessionStateMachine:
    """Control-truth for the Realtime session. UI phases stay presentation-only."""

    def __init__(
        self,
        *,
        on_transition: Callable[[SessionState, SessionState, str], None] | None = None,
        strict: bool = False,
    ) -> None:
        self._state = SessionState.DISCONNECTED
        self._on_transition = on_transition
        self._strict = strict

    @property
    def state(self) -> SessionState:
        return self._state

    def transition(self, new_state: SessionState, *, reason: str) -> bool:
        old_state = self._state
        if new_state is old_state:
            return True
        if new_state not in LEGAL_TRANSITIONS[old_state]:
            message = f"illegal transition {old_state.name} -> {new_state.name} ({reason})"
            if self._strict:
                raise AssertionError(message)
            logger.warning(message)
            return False
        self._state = new_state
        if self._on_transition is not None:
            try:
                self._on_transition(old_state, new_state, reason)
            except Exception:
                logger.exception("fsm transition listener failed")
        return True

    def accepts_user_audio(self) -> bool:
        return self._state in _ACCEPTS_USER_AUDIO

    def generation_active(self) -> bool:
        return self._state in _GENERATION_ACTIVE
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_session_fsm.py -v`
Expected: all PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add reachy_openai_realtime/session tests/test_session_fsm.py
git commit -m "feat: add explicit session state machine"
```

---

### Task 5: Integrate the FSM into `realtime.py` (replace state booleans)

The delicate task: `_input_enabled` and `_response_active` die; every reader switches to FSM queries. UI phases (`status.set_phase`) are presentation and stay exactly where they are.

**Files:**
- Modify: `reachy_openai_realtime/realtime.py` (`__init__`, `run`, `_run_connection`, `_record_loop`, `_event_loop`, `_interrupt_assistant`)
- Create: `tests/conftest.py` (FSM drive helper)
- Test: `tests/test_realtime_fsm.py` (new), `tests/test_realtime_manual_turn.py` (update)

**Interfaces:**
- Consumes: `SessionState`, `SessionStateMachine` from `reachy_openai_realtime.session.fsm` (Task 4).
- Produces:
  - `RealtimeRobotSession.fsm: SessionStateMachine` (constructed in `__init__` with `on_transition=self._on_fsm_transition`)
  - `RealtimeRobotSession._on_fsm_transition(old: SessionState, new: SessionState, reason: str) -> None` — records `fsm.transition` via the status recorder mirror
  - `RealtimeRobotSession._response_generation_done: bool` — True when no response is being generated server-side (set on `response.done`, cleared when a response is requested)
  - tests/conftest.py: `drive_fsm(fsm: SessionStateMachine, target: SessionState) -> None` — walks a legal path to any state (BFS over `LEGAL_TRANSITIONS`)

**State mapping** (the semantic contract for every edit below):

| Old check | New check |
|---|---|
| `self._input_enabled` (may start/continue a user turn) | `self.fsm.state in (SessionState.LISTENING, SessionState.USER_SPEAKING)` |
| `self._response_active` (barge-in-capable playback) | `self.fsm.state in (SessionState.ASSISTANT_SPEAKING, SessionState.INTERRUPTING)` |
| `self._response_active` (a response is in flight) | `self.fsm.generation_active()` |

**Transition sites** (who calls `transition()` and when):

```text
run(): CONNECTING          reason="connect_attempt"     each (re)connect loop iteration
_run_connection(): INITIALIZING reason="socket_open"    after connect() succeeds, before session.update
_event_loop() session.updated: LISTENING reason="session_updated"
_record_loop() VAD started:    USER_SPEAKING reason="vad_started"
_record_loop() commit+response.create: WAITING_RESPONSE reason="turn_committed"; _response_generation_done=False
greeting request (session.updated handler): WAITING_RESPONSE reason="greeting_requested"; _response_generation_done=False
_event_loop() first response.output_audio.delta of a response: ASSISTANT_SPEAKING reason="first_audio_received"
_event_loop() response.done, pending tool outputs: TOOL_EXECUTION reason="tool_outputs_pending",
    then after flush + response.create: WAITING_RESPONSE reason="tool_outputs_submitted"; _response_generation_done=False
_event_loop() response.done, no audio ever produced and speaker idle: LISTENING reason="response_completed"
_record_loop() speaker drained (state is ASSISTANT_SPEAKING, past _speaker_busy_until,
    _response_generation_done is True): LISTENING reason="playback_finished"
_interrupt_assistant() entry: INTERRUPTING reason="barge_in"
_interrupt_assistant() exit:  USER_SPEAKING reason="user_turn_continues"
run() on connection error:    RECOVERING reason=<short error class, e.g. "connection_error">
run() on stop_event:          STOPPING reason="stop_requested", then DISCONNECTED reason="shutdown_complete"
```

`_response_generation_done` starts `True` in `__init__`. It exists because `response.done` can arrive while audio is still draining — the FSM stays `ASSISTANT_SPEAKING` until playback finishes, and this flag is how the drain check knows the server is done.

- [ ] **Step 1: Map every read/write site.** Run and list results before editing:

```bash
rg -n "_input_enabled|_response_active" reachy_openai_realtime tests
```

Every hit must be accounted for by the mapping table above. If you find a site the table doesn't cover, stop and reconcile it against the transition-sites list before proceeding (do not invent a fourth mapping silently).

- [ ] **Step 2: Write the conftest helper and failing tests**

```python
# tests/conftest.py
# ABOUTME: Shared test fixtures/helpers. drive_fsm walks a session state machine
# ABOUTME: to a target state through legal transitions only (exercises the table).
from collections import deque

from reachy_openai_realtime.session.fsm import LEGAL_TRANSITIONS, SessionState, SessionStateMachine


def drive_fsm(fsm: SessionStateMachine, target: SessionState) -> None:
    """Walk fsm to target via a shortest legal path. Raises if unreachable."""
    if fsm.state is target:
        return
    frontier = deque([(fsm.state, [])])
    seen = {fsm.state}
    while frontier:
        state, path = frontier.popleft()
        for nxt in LEGAL_TRANSITIONS[state]:
            if nxt in seen:
                continue
            if nxt is target:
                for step in path + [nxt]:
                    assert fsm.transition(step, reason="test_drive")
                return
            seen.add(nxt)
            frontier.append((nxt, path + [nxt]))
    raise AssertionError(f"no legal path from {fsm.state} to {target}")
```

```python
# tests/test_realtime_fsm.py
import asyncio
import time

from conftest import drive_fsm

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.vad import EnergyTurnDetector

from test_realtime_manual_turn import FakeConnection, FakeMedia, FakeMotion, FakeStopEvent, stereo_frame


def make_session(frames, stop_event) -> RealtimeRobotSession:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": FakeMedia(frames)})()
    session.motion = FakeMotion()
    session.config = AppConfig()
    session.status = RuntimeStatus()
    session.connection = FakeConnection(stop_event)
    session.fsm = SessionStateMachine()
    session._response_generation_done = True
    session._playback_queue = asyncio.Queue()
    session._speaker_busy_until = time.monotonic() - 1.0
    session._camera_enabled_callback = lambda: False
    session._camera_capture_task = None
    session._last_camera_item_id = None
    session._pending_camera_items = {}
    session._camera_add_events = {}
    session._camera_delete_events = {}
    session._vad = EnergyTurnDetector()
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

    asyncio.run(session._record_loop(stop_event))

    assert session.connection.input_audio_buffer.committed == 1
    assert session.connection.response.created == 1
    assert session.fsm.state is SessionState.WAITING_RESPONSE
    assert session._response_generation_done is False


def test_frames_ignored_while_waiting_for_response() -> None:
    stop_event = FakeStopEvent()
    frames = [stereo_frame(-30.0) for _ in range(15)]
    session = make_session(frames, stop_event)
    drive_fsm(session.fsm, SessionState.WAITING_RESPONSE)

    asyncio.run(session._record_loop(stop_event))

    assert session.connection.input_audio_buffer.appended == 0
    assert session.connection.input_audio_buffer.committed == 0
    assert session.fsm.state is SessionState.WAITING_RESPONSE
```

(The FakeStopEvent in the second test never gets set by a `response.create`, so add a frame-exhaustion guard exactly like the existing tests rely on: `_record_loop` already exits when `get_audio_sample` returns `None`.)

- [ ] **Step 3: Run new tests to verify they fail**

Run: `uv run pytest tests/test_realtime_fsm.py -v`
Expected: FAIL (`AttributeError: fsm` / record loop still consults `_input_enabled`)

- [ ] **Step 4: Implement in `realtime.py`.**
  - `from .session.fsm import SessionState, SessionStateMachine`
  - In `__init__`: delete `self._input_enabled = False` and `self._response_active = False`; add:

```python
self.fsm = SessionStateMachine(on_transition=self._on_fsm_transition)
self._response_generation_done = True
```

  - Add the listener (records through the status mirror so `events.jsonl` gets `fsm.transition` entries once a recorder is attached):

```python
def _on_fsm_transition(self, old_state: SessionState, new_state: SessionState, reason: str) -> None:
    self.status.record_event(
        "fsm.transition", from_state=old_state.name, to_state=new_state.name, reason=reason
    )
```

  (`RuntimeStatus.record_event` is the public forwarding helper defined in Task 3.)
  - Apply the state-mapping table to every site found in Step 1, and add every transition from the transition-sites list. The record-loop gating logic becomes:

```python
state = self.fsm.state
if state in (SessionState.LISTENING, SessionState.USER_SPEAKING):
    process_turn = True          # existing VAD/append/commit path
    barge_in_watch = False
elif state in (SessionState.ASSISTANT_SPEAKING, SessionState.INTERRUPTING):
    process_turn = True          # VAD runs, but a start triggers _interrupt_assistant
    barge_in_watch = True        # existing "assistant_audio_active" semantics
else:
    self._vad.reset_turn()       # WAITING_RESPONSE / TOOL_EXECUTION / teardown states
    continue                     # keep draining mic frames, send nothing
```

    matching the existing loop's structure (the current code expresses `barge_in_watch` as `assistant_audio_active`; keep that local name).
  - In the drain check that currently waits on `_speaker_busy_until`: when `state is ASSISTANT_SPEAKING and self._response_generation_done and time.monotonic() >= self._speaker_busy_until`, transition `LISTENING, reason="playback_finished"`.
  - `response.created` handler: set `self._current_response_id` (existing) — no transition (already `WAITING_RESPONSE`).
  - `response.done` handler: set `self._response_generation_done = True` before the existing tool-flush/interrupted-response logic; add the `TOOL_EXECUTION`/`LISTENING` transitions per the sites list.
  - `_interrupt_assistant`: first line `self.fsm.transition(SessionState.INTERRUPTING, reason="barge_in")`, last line `self.fsm.transition(SessionState.USER_SPEAKING, reason="user_turn_continues")`.
  - `run()`/`_run_connection`: add `CONNECTING`/`INITIALIZING`/`RECOVERING`/`STOPPING`/`DISCONNECTED` transitions per the sites list (the reconnect loop itself is rewritten in Task 7 — here, add transitions to the *existing* loop shape without changing retry behavior).

- [ ] **Step 5: Update the existing manual-turn tests.** In `tests/test_realtime_manual_turn.py`, for each test constructing a session by hand:
  - Add `from conftest import drive_fsm` and `from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine`.
  - Replace `session._input_enabled = True` / `session._response_active = False` with:

```python
session.fsm = SessionStateMachine()
session._response_generation_done = True
drive_fsm(session.fsm, SessionState.LISTENING)
```

  - Replace `session._input_enabled = False` / `session._response_active = True` (the two barge-in tests) with:

```python
session.fsm = SessionStateMachine()
session._response_generation_done = False
drive_fsm(session.fsm, SessionState.ASSISTANT_SPEAKING)
```

  - `test_barge_in_cancels_clears_and_truncates_at_played_audio` gains one assertion after the existing ones: `assert session.fsm.state is SessionState.USER_SPEAKING`.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS, including the untouched camera tests (they never touched the booleans)

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check .
git add reachy_openai_realtime/realtime.py reachy_openai_realtime/runtime_status.py tests/conftest.py tests/test_realtime_fsm.py tests/test_realtime_manual_turn.py
git commit -m "feat: replace session state booleans with explicit FSM"
```

---

### Task 6: Connection epochs, canonical reset, bounded interrupted-IDs

**Files:**
- Modify: `reachy_openai_realtime/realtime.py`
- Test: `tests/test_realtime_reset.py`

**Interfaces:**
- Consumes: `SessionState` (Task 4), FSM integration (Task 5).
- Produces:
  - `RealtimeRobotSession.connection_epoch: int` — starts 0, incremented once per connection attempt (Task 7 moves the increment into the rewritten `run()`; until then it lives at the top of `_run_connection`)
  - `class RecentIds:` (module-level in `realtime.py`) — `__init__(self, max_size: int = 32)`, `add(self, value: str) -> None`, `__contains__`, `__len__`, `clear(self) -> None`
  - `RealtimeRobotSession.reset_connection_state() -> None` (async) — the spec §4 checklist, callable any time between connections
  - `_pending_tool_outputs: list[tuple[int, str, str]]` — now `(epoch, call_id, output_json)`; the flush in the `response.done` handler skips entries whose epoch != `self.connection_epoch`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_realtime_reset.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_realtime_reset.py -v`
Expected: FAIL (`ImportError: RecentIds` / `AttributeError: reset_connection_state`)

- [ ] **Step 3: Implement.** In `realtime.py`:

```python
class RecentIds:
    """Bounded remembered-ID set (spec §27: interrupted response IDs must not grow forever)."""

    def __init__(self, max_size: int = 32) -> None:
        self._order: deque[str] = deque()
        self._members: set[str] = set()
        self._max_size = max_size

    def add(self, value: str) -> None:
        if value in self._members:
            return
        if len(self._order) >= self._max_size:
            self._members.discard(self._order.popleft())
        self._order.append(value)
        self._members.add(value)

    def __contains__(self, value: object) -> bool:
        return value in self._members

    def __len__(self) -> int:
        return len(self._order)

    def clear(self) -> None:
        self._order.clear()
        self._members.clear()
```

  - `__init__`: `self.connection_epoch = 0`; `self._interrupted_response_ids = RecentIds()` (was a bare `set`); `_pending_tool_outputs` typed `list[tuple[int, str, str]]`.
  - Top of `_run_connection`: `self.connection_epoch += 1`.
  - Tool-call handler (`_handle_tool_call` result append): append `(self.connection_epoch, call_id, output_json)` instead of `(call_id, output_json)`; the flush loop in the `response.done` handler unpacks the triple and skips stale epochs.
  - `_interrupt_assistant`: `self._interrupted_response_ids.add(...)` (unchanged call, new type) and add an epoch guard at the top:

```python
epoch = self.connection_epoch
...  # existing cancel/clear/truncate awaits
if epoch != self.connection_epoch:
    return  # connection turned over mid-interrupt; new epoch owns the state
```

  - Add the canonical reset (async because Task 8+ hooks and future camera-task cancellation need await points; today it awaits only the queue drain):

```python
async def reset_connection_state(self) -> None:
    """Spec §4: a reconnect must never inherit partially active response state."""
    if self._camera_capture_task is not None:
        self._camera_capture_task.cancel()
        self._camera_capture_task = None
    while not self._playback_queue.empty():
        try:
            self._playback_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    try:
        self.motion.stop_current()
    except Exception:
        logger.exception("motion stop during reset failed")
    self._vad.reset_turn()
    self._pending_tool_outputs.clear()
    self._interrupted_response_ids.clear()
    self._current_response_id = None
    self._current_audio_item_id = None
    self._current_audio_content_index = 0
    self._playback_started_at = None
    self._playback_pushed_ms = 0.0
    self._speaker_busy_until = time.monotonic()
    self._pending_camera_items.clear()
    self._camera_add_events.clear()
    self._camera_delete_events.clear()
    self._last_camera_item_id = None
    self._response_generation_done = True
```

    ("mark input disabled / response inactive" from the spec checklist is the FSM's job — the caller transitions to `RECOVERING`/`CONNECTING` around this call; "remove connection-specific timers" gains `self.watchdog.clear()` in Task 8.)
  - Call `await self.reset_connection_state()` in `run()`'s exception path (the existing retry loop), before sleeping/reconnecting.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS (barge-in tests updated in Task 5 already construct `RecentIds()` via `session._interrupted_response_ids = set()` — change those two lines to `RecentIds()` as part of this task; grep: `rg -n "_interrupted_response_ids" tests`)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check .
git add reachy_openai_realtime/realtime.py tests/test_realtime_reset.py tests/test_realtime_manual_turn.py
git commit -m "feat: add connection epochs, canonical reset, bounded interrupted-ids"
```

---

### Task 7: Reconnect loop with jittered backoff and fatal-error classification

**Files:**
- Create: `reachy_openai_realtime/session/recovery.py`
- Modify: `reachy_openai_realtime/realtime.py` (`run()` rewrite)
- Modify: `reachy_openai_realtime/config.py` (remove `reconnect_attempts`)
- Modify: `reachy_openai_realtime/main.py` (handle `SessionOutcome.FATAL_CONFIG`)
- Test: `tests/test_session_recovery.py`, `tests/test_realtime_reconnect.py`

**Interfaces:**
- Consumes: FSM (Task 5), `reset_connection_state` (Task 6), `RuntimeStatus.record_event` (Task 5 rename).
- Produces (from `reachy_openai_realtime.session.recovery`):
  - `class BackoffPolicy:`
    - `DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)`
    - `__init__(self, *, jitter_ratio: float = 0.2, healthy_reset_seconds: float = 60.0, rng: random.Random | None = None) -> None`
    - `note_session_duration(self, seconds: float) -> None` — resets the attempt counter when `seconds >= healthy_reset_seconds`
    - `next_delay(self) -> float` — advances the counter; returns `DELAYS[min(attempt, last)] ± jitter_ratio`
    - `reset(self) -> None`
  - `class ErrorClass(Enum): TRANSIENT; FATAL_CONFIG`
  - `classify_connection_error(exc: BaseException) -> ErrorClass`
  - `class SessionOutcome(Enum): STOPPED; FATAL_CONFIG`
- `RealtimeRobotSession.run(stop_event) -> SessionOutcome` (was implicitly `None`/raise)

- [ ] **Step 1: Write the failing unit tests**

```python
# tests/test_session_recovery.py
import random

from reachy_openai_realtime.session.recovery import (
    BackoffPolicy,
    ErrorClass,
    classify_connection_error,
)


def test_backoff_sequence_caps_at_thirty_seconds() -> None:
    policy = BackoffPolicy(jitter_ratio=0.0)
    delays = [policy.next_delay() for _ in range(8)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 30.0, 30.0]


def test_jitter_stays_within_twenty_percent() -> None:
    policy = BackoffPolicy(rng=random.Random(7))
    for expected in BackoffPolicy.DELAYS:
        delay = policy.next_delay()
        assert expected * 0.8 <= delay <= expected * 1.2


def test_healthy_session_resets_backoff() -> None:
    policy = BackoffPolicy(jitter_ratio=0.0)
    for _ in range(4):
        policy.next_delay()
    policy.note_session_duration(61.0)
    assert policy.next_delay() == 1.0


def test_short_session_does_not_reset_backoff() -> None:
    policy = BackoffPolicy(jitter_ratio=0.0)
    policy.next_delay()
    policy.note_session_duration(5.0)
    assert policy.next_delay() == 2.0


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class AuthenticationError(Exception):
    pass


def test_classification_table() -> None:
    assert classify_connection_error(_StatusError(401)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(403)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(404)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(422)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(429)) is ErrorClass.TRANSIENT
    assert classify_connection_error(_StatusError(500)) is ErrorClass.TRANSIENT
    assert classify_connection_error(_StatusError(503)) is ErrorClass.TRANSIENT
    assert classify_connection_error(ConnectionError("reset")) is ErrorClass.TRANSIENT
    assert classify_connection_error(OSError("network down")) is ErrorClass.TRANSIENT
    # SDK exception types are matched by name when no status code is exposed
    assert classify_connection_error(AuthenticationError("bad key")) is ErrorClass.FATAL_CONFIG


def test_nested_response_status_is_found() -> None:
    class Handshake(Exception):
        def __init__(self) -> None:
            super().__init__("rejected")
            self.response = type("Resp", (), {"status_code": 401})()

    assert classify_connection_error(Handshake()) is ErrorClass.FATAL_CONFIG
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session_recovery.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `session/recovery.py`**

```python
# reachy_openai_realtime/session/recovery.py
# ABOUTME: Reconnect backoff policy and connection-error classification
# ABOUTME: (spec §5): transient errors retry forever; config errors stop.
from __future__ import annotations

import random
from enum import Enum, auto


class BackoffPolicy:
    """Jittered exponential backoff, reset after a healthy connection."""

    DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

    def __init__(
        self,
        *,
        jitter_ratio: float = 0.2,
        healthy_reset_seconds: float = 60.0,
        rng: random.Random | None = None,
    ) -> None:
        self._jitter_ratio = jitter_ratio
        self._healthy_reset_seconds = healthy_reset_seconds
        self._rng = rng or random.Random()
        self._attempt = 0

    def note_session_duration(self, seconds: float) -> None:
        if seconds >= self._healthy_reset_seconds:
            self.reset()

    def next_delay(self) -> float:
        base = self.DELAYS[min(self._attempt, len(self.DELAYS) - 1)]
        self._attempt += 1
        if self._jitter_ratio <= 0.0:
            return base
        return base * self._rng.uniform(1.0 - self._jitter_ratio, 1.0 + self._jitter_ratio)

    def reset(self) -> None:
        self._attempt = 0


class ErrorClass(Enum):
    TRANSIENT = auto()
    FATAL_CONFIG = auto()


class SessionOutcome(Enum):
    STOPPED = auto()
    FATAL_CONFIG = auto()


_FATAL_EXCEPTION_NAMES = frozenset(
    {"AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError",
     "UnprocessableEntityError"}
)


def _find_status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    for attribute in ("status_code", "status"):
        status = getattr(response, attribute, None)
        if isinstance(status, int):
            return status
    return None


def classify_connection_error(exc: BaseException) -> ErrorClass:
    """Spec §5: auth/model/config failures must not cause reconnect spam.

    Matches by HTTP status where the exception exposes one (openai SDK and
    websocket handshake errors both do), falling back to SDK exception names.
    Unknown errors default to TRANSIENT — robustness beats giving up.
    """
    status = _find_status_code(exc)
    if status is not None:
        if status == 429:
            return ErrorClass.TRANSIENT
        if 400 <= status < 500:
            return ErrorClass.FATAL_CONFIG
        return ErrorClass.TRANSIENT
    if type(exc).__name__ in _FATAL_EXCEPTION_NAMES:
        return ErrorClass.FATAL_CONFIG
    return ErrorClass.TRANSIENT
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `uv run pytest tests/test_session_recovery.py -v`
Expected: all PASS

- [ ] **Step 5: Write the failing reconnect-loop test**

```python
# tests/test_realtime_reconnect.py
import asyncio
import threading

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.recovery import SessionOutcome


class FatalConnectError(Exception):
    def __init__(self) -> None:
        super().__init__("invalid key")
        self.status_code = 401


def make_session(connect_error: Exception, attempts: list[int]) -> RealtimeRobotSession:
    session = RealtimeRobotSession(
        client=None,  # replaced below; constructor signature per realtime.py
        robot=None,
        config=AppConfig(),
        status=RuntimeStatus(),
    )

    async def failing_run_connection(stop_event) -> None:
        attempts.append(session.connection_epoch)
        raise connect_error

    session._run_connection = failing_run_connection  # type: ignore[method-assign]
    return session


def test_fatal_error_stops_reconnecting_immediately() -> None:
    attempts: list[int] = []
    session = make_session(FatalConnectError(), attempts)
    stop_event = threading.Event()

    outcome = asyncio.run(session.run(stop_event))

    assert outcome is SessionOutcome.FATAL_CONFIG
    assert attempts == [1]


def test_transient_error_retries_with_new_epoch_until_stop() -> None:
    attempts: list[int] = []
    stop_event = threading.Event()

    class TransientError(ConnectionError):
        pass

    session = make_session(TransientError("wifi blip"), attempts)
    original_sleep = session._sleep_unless_stopped

    async def fast_sleep(event, seconds: float) -> None:
        if len(attempts) >= 3:
            event.set()
        await original_sleep(event, 0.0)

    session._sleep_unless_stopped = fast_sleep  # type: ignore[method-assign]
    outcome = asyncio.run(session.run(stop_event))

    assert outcome is SessionOutcome.STOPPED
    assert attempts == [1, 2, 3]
```

The real `__init__` signature (realtime.py:101) is `(robot, motion, config, status, language_provider=None, camera_enabled=None, capture_camera_jpeg=None)`, and it constructs `AsyncOpenAI()` internally — so `os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-0000000000")` first, then `RealtimeRobotSession(fake_robot, BargeInMotion(), AppConfig(), RuntimeStatus())` (fakes from the manual-turn module; the mocked `_run_connection` never touches robot or client).

- [ ] **Step 6: Implement the `run()` rewrite in `realtime.py`**

```python
async def run(self, stop_event) -> SessionOutcome:
    backoff = BackoffPolicy()
    while not stop_event.is_set():
        self.connection_epoch += 1
        self.fsm.transition(SessionState.CONNECTING, reason="connect_attempt")
        self.status.record_event("realtime.connecting", epoch=self.connection_epoch)
        connected_at = time.monotonic()
        error: BaseException | None = None
        try:
            await self._run_connection(stop_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        if stop_event.is_set():
            break
        self.fsm.transition(SessionState.RECOVERING, reason="connection_lost")
        await self.reset_connection_state()
        if error is not None:
            self.status.record_error(f"realtime connection failed: {error}")
            if classify_connection_error(error) is ErrorClass.FATAL_CONFIG:
                self.status.set_phase("error", detail_key="config_error", connected=False)
                self.status.record_event("realtime.error", fatal=True, message=str(error))
                return SessionOutcome.FATAL_CONFIG
        backoff.note_session_duration(time.monotonic() - connected_at)
        delay = backoff.next_delay()
        self.status.record_event("realtime.reconnect", delay_seconds=round(delay, 2))
        await self._sleep_unless_stopped(stop_event, delay)
    self.fsm.transition(SessionState.STOPPING, reason="stop_requested")
    await self.reset_connection_state()
    self.fsm.transition(SessionState.DISCONNECTED, reason="shutdown_complete")
    return SessionOutcome.STOPPED


async def _sleep_unless_stopped(self, stop_event, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.2, remaining))
```

  Match the existing `set_phase` call conventions in `realtime.py` for the config-error phase (read neighboring `set_phase` calls and use the same parameter style; the i18n `detail_key` machinery already exists — reuse an existing error-ish key if `config_error` isn't defined yet, and note the raw-message fallback: `add_event` with no key). Remove the old `for attempt in range(...)` loop, the `2 ** (attempt - 1)` sleep, and the terminal `RuntimeError`. Move the Task 6 epoch increment from `_run_connection` into this loop (delete the old line).

- [ ] **Step 7: Remove `reconnect_attempts` from `config.py`.** Delete the field from `AppConfig` and its `from_env` line. Grep for stragglers:

```bash
rg -n "reconnect_attempts" reachy_openai_realtime tests
```

Update any test constructing `AppConfig(reconnect_attempts=...)`.

- [ ] **Step 8: Handle `FATAL_CONFIG` in `main.py`.** In the run loop where `asyncio.run(session.run(stop_event))` is called, capture the outcome. On `SessionOutcome.FATAL_CONFIG`, wait for a configuration change before retrying (mirrors the existing API-key wait loop):

```python
outcome = asyncio.run(session.run(stop_event))
if outcome is SessionOutcome.FATAL_CONFIG:
    stale_fingerprint = (os.getenv("OPENAI_API_KEY", ""), config)
    while not stop_event.is_set():
        load_instance_env()  # picks up UI-driven .env edits, matching startup behavior
        current = (os.getenv("OPENAI_API_KEY", ""), AppConfig.from_env())
        if current != stale_fingerprint:
            break
        stop_event.wait(2.0)
```

(`AppConfig` is a frozen dataclass, so tuple equality works. Reuse the module's existing imports/names — `load_instance_env` is already imported in `main.py` for startup.)

- [ ] **Step 9: Run the full suite, lint, commit**

```bash
uv run pytest -v && uv run ruff check .
git add reachy_openai_realtime/session/recovery.py reachy_openai_realtime/realtime.py reachy_openai_realtime/config.py reachy_openai_realtime/main.py tests/test_session_recovery.py tests/test_realtime_reconnect.py tests/test_realtime_config.py
git commit -m "feat: infinite jittered reconnect with fatal config-error classification"
```

---

### Task 8: Protocol watchdog deadlines (`session/watchdog.py`)

**Files:**
- Create: `reachy_openai_realtime/session/watchdog.py`
- Modify: `reachy_openai_realtime/realtime.py` (arm/disarm sites + watchdog task in `_run_connection`, `reset_connection_state`)
- Test: `tests/test_session_watchdog.py`

**Interfaces:**
- Consumes: FSM/reconnect machinery (Tasks 5–7): a `WatchdogTimeout` escaping `_run_connection` is classified TRANSIENT by `classify_connection_error` (no status code, unknown name) and triggers reset + backoff — exactly the spec §5 recovery sequence.
- Produces (from `reachy_openai_realtime.session.watchdog`):
  - `DEFAULT_DEADLINES: dict[str, float]` = `{"session_update": 5.0, "response_create": 5.0, "first_output": 15.0, "response_cancel": 3.0, "tool_response": 5.0, "input_append": 5.0, "camera_item": 5.0}`
  - `class WatchdogTimeout(RuntimeError):` with attributes `operation: str`, `timeout_seconds: float`
  - `class DeadlineWatchdog:`
    - `__init__(self, *, clock: Callable[[], float] = time.monotonic) -> None`
    - `arm(self, operation: str, timeout_seconds: float | None = None) -> None` — `None` looks up `DEFAULT_DEADLINES[operation]`; re-arming replaces the deadline
    - `disarm(self, operation: str) -> None` — unknown operation is a no-op
    - `clear(self) -> None`
    - `expired(self) -> tuple[str, float] | None` — earliest-expired `(operation, timeout_seconds)` or None
    - `async watch(self, interval_seconds: float = 0.25) -> None` — polls forever; raises `WatchdogTimeout` on expiry
  - `RealtimeRobotSession.watchdog: DeadlineWatchdog` (constructed in `__init__`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_session_watchdog.py
import asyncio

import pytest

from reachy_openai_realtime.session.watchdog import (
    DEFAULT_DEADLINES,
    DeadlineWatchdog,
    WatchdogTimeout,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_expired_after_deadline_passes() -> None:
    clock = FakeClock()
    watchdog = DeadlineWatchdog(clock=clock)
    watchdog.arm("response_create")
    assert watchdog.expired() is None
    clock.now += DEFAULT_DEADLINES["response_create"] + 0.1
    assert watchdog.expired() == ("response_create", DEFAULT_DEADLINES["response_create"])


def test_disarm_prevents_expiry_and_unknown_disarm_is_noop() -> None:
    clock = FakeClock()
    watchdog = DeadlineWatchdog(clock=clock)
    watchdog.arm("session_update")
    watchdog.disarm("session_update")
    watchdog.disarm("never_armed")
    clock.now += 60.0
    assert watchdog.expired() is None


def test_rearm_replaces_deadline() -> None:
    clock = FakeClock()
    watchdog = DeadlineWatchdog(clock=clock)
    watchdog.arm("first_output", 15.0)
    clock.now += 10.0
    watchdog.arm("first_output", 15.0)
    clock.now += 10.0
    assert watchdog.expired() is None
    clock.now += 6.0
    assert watchdog.expired() is not None


def test_earliest_expiry_wins() -> None:
    clock = FakeClock()
    watchdog = DeadlineWatchdog(clock=clock)
    watchdog.arm("first_output", 15.0)
    watchdog.arm("response_cancel", 3.0)
    clock.now += 20.0
    operation, _ = watchdog.expired()
    assert operation == "response_cancel"


def test_watch_raises_watchdog_timeout() -> None:
    clock = FakeClock()
    watchdog = DeadlineWatchdog(clock=clock)
    watchdog.arm("response_cancel", 3.0)
    clock.now += 5.0

    async def run() -> None:
        await watchdog.watch(interval_seconds=0.01)

    with pytest.raises(WatchdogTimeout) as excinfo:
        asyncio.run(run())
    assert excinfo.value.operation == "response_cancel"
    assert excinfo.value.timeout_seconds == 3.0


def test_clear_disarms_everything() -> None:
    clock = FakeClock()
    watchdog = DeadlineWatchdog(clock=clock)
    watchdog.arm("session_update")
    watchdog.arm("camera_item")
    watchdog.clear()
    clock.now += 60.0
    assert watchdog.expired() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session_watchdog.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# reachy_openai_realtime/session/watchdog.py
# ABOUTME: Expectation-driven protocol deadlines (spec §5). A missed deadline
# ABOUTME: raises WatchdogTimeout, tearing down the connection for a clean rebuild.
from __future__ import annotations

import asyncio
import time
from typing import Callable

DEFAULT_DEADLINES: dict[str, float] = {
    "session_update": 5.0,
    "response_create": 5.0,
    "first_output": 15.0,
    "response_cancel": 3.0,
    "tool_response": 5.0,
    "input_append": 5.0,
    "camera_item": 5.0,
}


class WatchdogTimeout(RuntimeError):
    def __init__(self, operation: str, timeout_seconds: float) -> None:
        super().__init__(f"watchdog deadline expired: {operation} after {timeout_seconds:.1f}s")
        self.operation = operation
        self.timeout_seconds = timeout_seconds


class DeadlineWatchdog:
    """Tracks armed protocol deadlines against an injectable monotonic clock."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._deadlines: dict[str, tuple[float, float]] = {}  # op -> (deadline_at, timeout)

    def arm(self, operation: str, timeout_seconds: float | None = None) -> None:
        timeout = DEFAULT_DEADLINES[operation] if timeout_seconds is None else timeout_seconds
        self._deadlines[operation] = (self._clock() + timeout, timeout)

    def disarm(self, operation: str) -> None:
        self._deadlines.pop(operation, None)

    def clear(self) -> None:
        self._deadlines.clear()

    def expired(self) -> tuple[str, float] | None:
        now = self._clock()
        earliest: tuple[str, float] | None = None
        earliest_at = float("inf")
        for operation, (deadline_at, timeout) in self._deadlines.items():
            if deadline_at <= now and deadline_at < earliest_at:
                earliest = (operation, timeout)
                earliest_at = deadline_at
        return earliest

    async def watch(self, interval_seconds: float = 0.25) -> None:
        while True:
            hit = self.expired()
            if hit is not None:
                raise WatchdogTimeout(hit[0], hit[1])
            await asyncio.sleep(interval_seconds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_session_watchdog.py -v`
Expected: all PASS

- [ ] **Step 5: Integrate into `realtime.py`.**
  - `__init__`: `self.watchdog = DeadlineWatchdog()`.
  - `_run_connection`: add a fourth task to the existing `asyncio.gather(...)` alongside record/playback/event loops:

```python
async def _watchdog_loop(self) -> None:
    try:
        await self.watchdog.watch()
    except WatchdogTimeout as exc:
        self.status.record_event(
            "watchdog.triggered", operation=exc.operation, timeout_seconds=exc.timeout_seconds
        )
        self.status.add_event("warning", f"protocol watchdog: {exc.operation} timed out")
        raise
```

    The existing finally-block in `_run_connection` already cancels sibling tasks when one raises — the watchdog rides that proven mechanism.
  - Arm/disarm sites (each is one line; find the exact anchors by the described operation):
    - after `await connection.session.update(...)`: `arm("session_update")`; in the `session.updated` handler: `disarm("session_update")`.
    - immediately before every `response.create` await (turn commit, greeting, tool-output flush): `arm("response_create")`; in the `response.created` handler: `disarm("response_create")`, `disarm("tool_response")`, `arm("first_output")`.
    - in the `response.output_audio.delta` handler and the `response.function_call_arguments.done` handler: `disarm("first_output")`.
    - in the `response.done` handler: `disarm("first_output")`, `disarm("response_cancel")` (GA emits `response.done` for cancelled responses too; if a distinct `response.cancelled` event case exists or is added, disarm there as well).
    - in `_interrupt_assistant`, after the `response.cancel(...)` await: `arm("response_cancel")`.
    - around the commit block in `_record_loop` (before `input_audio_buffer.commit()`): `arm("input_append")`; after the following `response.create` returns: `disarm("input_append")`.
    - in `_capture_and_send_camera_image`, before `conversation.item.create(...)`: `arm("camera_item")`; in `_confirm_camera_item` and `_handle_camera_protocol_error`: `disarm("camera_item")`.
    - tool-output flush in `response.done` handler, after sending outputs + `response.create`: `arm("tool_response")` (cleared by the next `response.created`, above).
  - `reset_connection_state`: add `self.watchdog.clear()` (this is the spec checklist line "remove connection-specific timers").

- [ ] **Step 6: Write one integration test** (append to `tests/test_session_watchdog.py`):

```python
def test_watchdog_loop_records_event_and_reraises() -> None:
    from reachy_openai_realtime.realtime import RealtimeRobotSession
    from reachy_openai_realtime.runtime_status import RuntimeStatus
    from reachy_openai_realtime.session.watchdog import DeadlineWatchdog

    clock = FakeClock()
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.watchdog = DeadlineWatchdog(clock=clock)
    session.watchdog.arm("response_create")
    clock.now += 10.0

    with pytest.raises(WatchdogTimeout):
        asyncio.run(session._watchdog_loop())
```

- [ ] **Step 7: Run the full suite, lint, commit**

```bash
uv run pytest -v && uv run ruff check .
git add reachy_openai_realtime/session/watchdog.py reachy_openai_realtime/realtime.py tests/test_session_watchdog.py
git commit -m "feat: add protocol deadline watchdog with connection teardown on expiry"
```

---

### Task 9: Audio capture worker + mic recovery ladder (and `audio.py` → `dsp.py` rename)

The spec's `audio/` package (§28) collides with the existing `audio.py` module, so the pure-DSP helpers move to `dsp.py` first, then the new package hosts the capture worker.

**Files:**
- Rename: `reachy_openai_realtime/audio.py` → `reachy_openai_realtime/dsp.py` (git mv)
- Rename: `tests/test_audio.py` → `tests/test_dsp.py` (git mv)
- Create: `reachy_openai_realtime/audio/__init__.py` (empty), `reachy_openai_realtime/audio/capture.py`
- Modify: `reachy_openai_realtime/realtime.py` (record loop, `run()` worker lifecycle, restart helpers), `reachy_openai_realtime/main.py` (AudioPipelineStalled escalation)
- Test: `tests/test_audio_capture.py`; update `tests/test_realtime_fsm.py`, `tests/test_realtime_manual_turn.py`

**Interfaces:**
- Consumes: FSM record-loop gating (Task 5), `run()` loop (Task 7), `RuntimeStatus.record_event` (Task 3).
- Produces (from `reachy_openai_realtime.audio.capture`):
  - `class AudioPipelineStalled(RuntimeError)` — raised when the ladder exhausts; must escape `RealtimeRobotSession.run()` (NOT treated as a reconnectable error) so `main.py` can rebuild the whole app session.
  - `class CaptureWorker:`
    - `__init__(self, media: Any, *, max_buffer_ms: float = 500.0) -> None`
    - `start(self) -> None` / `close(self) -> None` (bounded `join(timeout=2.0)`)
    - `pop(self, timeout_seconds: float) -> np.ndarray | None` — blocking; call via `asyncio.to_thread`
    - `frame_age_seconds(self) -> float`
    - attrs: `last_frame_at: float`, `frames_total: int`, `dropped_frames: int`
  - `class AudioRecoveryLadder:`
    - `__init__(self, *, stall_seconds: float = 1.75, cooldown_seconds: float = 3.0, clock: Callable[[], float] = time.monotonic) -> None`
    - `next_action(self, frame_age_seconds: float) -> str | None` — returns `"restart_capture"`, `"restart_media"`, `"restart_session"`, or None; healthy frames reset the ladder; a cooldown separates attempts
- `RealtimeRobotSession._capture: CaptureWorker` (created+started at the top of `run()`, closed in its `finally`), `RealtimeRobotSession._mic_ladder: AudioRecoveryLadder` (in `__init__`)

- [ ] **Step 1: Rename safety sweep.** Run each; every hit gets updated in Step 2:

```bash
rg -n "from \.audio import|from reachy_openai_realtime\.audio import" reachy_openai_realtime tests
rg -n "reachy_openai_realtime\.audio\b" reachy_openai_realtime tests docs README.md pyproject.toml
rg -n "\btest_audio\b" tests pyproject.toml
```

(Expect hits in `realtime.py` and `tests/test_audio.py`; there are no dynamic imports or entry points touching it — verify, don't assume.)

- [ ] **Step 2: Perform the rename**

```bash
git mv reachy_openai_realtime/audio.py reachy_openai_realtime/dsp.py
git mv tests/test_audio.py tests/test_dsp.py
```

Update every import found in Step 1 (`from .audio import ...` → `from .dsp import ...`; `from reachy_openai_realtime.audio import ...` → `from reachy_openai_realtime.dsp import ...`). Run `uv run pytest tests/test_dsp.py -v` — all PASS. Commit the rename alone:

```bash
git add -u && git commit -m "refactor: rename audio.py to dsp.py to free the audio package name"
```

- [ ] **Step 3: Write the failing capture tests**

```python
# tests/test_audio_capture.py
import queue
import threading
import time

import numpy as np
import pytest

from reachy_openai_realtime.audio.capture import (
    AudioPipelineStalled,
    AudioRecoveryLadder,
    CaptureWorker,
)


class ScriptedMedia:
    """Feed-controlled fake media; get_audio_sample drains one queued frame."""

    def __init__(self) -> None:
        self._frames: queue.Queue[np.ndarray] = queue.Queue()

    def feed(self, frame: np.ndarray) -> None:
        self._frames.put(frame)

    def get_audio_sample(self) -> np.ndarray | None:
        try:
            return self._frames.get_nowait()
        except queue.Empty:
            return None

    def get_input_audio_samplerate(self) -> int:
        return 16_000


def frame_of_ms(ms: float) -> np.ndarray:
    samples = int(16_000 * ms / 1000.0)
    return np.zeros((samples, 2), dtype=np.float32)


def test_pop_returns_fed_frames_in_order() -> None:
    media = ScriptedMedia()
    worker = CaptureWorker(media)
    worker.start()
    try:
        first = frame_of_ms(20.0)
        first[0, 0] = 1.0
        media.feed(first)
        media.feed(frame_of_ms(20.0))
        popped = worker.pop(1.0)
        assert popped is not None
        assert popped[0, 0] == 1.0
        assert worker.pop(1.0) is not None
        assert worker.frames_total == 2
    finally:
        worker.close()


def test_backlog_drops_oldest_beyond_budget() -> None:
    media = ScriptedMedia()
    worker = CaptureWorker(media, max_buffer_ms=100.0)
    worker.start()
    try:
        for _ in range(50):
            media.feed(frame_of_ms(20.0))
        deadline = time.monotonic() + 2.0
        while worker.frames_total < 50 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.frames_total == 50
        assert worker.dropped_frames > 0
        remaining = 0
        while worker.pop(0.05) is not None:
            remaining += 1
        assert remaining <= 6  # ~100ms budget of 20ms frames (+1 in flight)
    finally:
        worker.close()


def test_pop_times_out_without_frames() -> None:
    worker = CaptureWorker(ScriptedMedia())
    worker.start()
    try:
        started = time.monotonic()
        assert worker.pop(0.1) is None
        assert time.monotonic() - started < 1.0
    finally:
        worker.close()


def test_close_joins_thread() -> None:
    worker = CaptureWorker(ScriptedMedia())
    worker.start()
    before = threading.active_count()
    worker.close()
    assert threading.active_count() == before - 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def test_ladder_escalates_through_actions_with_cooldown() -> None:
    clock = FakeClock()
    ladder = AudioRecoveryLadder(stall_seconds=1.5, cooldown_seconds=3.0, clock=clock)
    assert ladder.next_action(0.2) is None
    assert ladder.next_action(2.0) == "restart_capture"
    assert ladder.next_action(2.5) is None  # cooldown
    clock.now += 4.0
    assert ladder.next_action(6.0) == "restart_media"
    clock.now += 4.0
    assert ladder.next_action(10.0) == "restart_session"
    clock.now += 4.0
    assert ladder.next_action(14.0) == "restart_session"  # stays at final rung


def test_ladder_resets_on_healthy_frames() -> None:
    clock = FakeClock()
    ladder = AudioRecoveryLadder(stall_seconds=1.5, cooldown_seconds=0.0, clock=clock)
    assert ladder.next_action(2.0) == "restart_capture"
    assert ladder.next_action(0.1) is None  # healthy → reset
    assert ladder.next_action(2.0) == "restart_capture"


def test_audio_pipeline_stalled_is_a_runtime_error() -> None:
    with pytest.raises(RuntimeError):
        raise AudioPipelineStalled("mic dead")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_audio_capture.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 5: Implement `audio/capture.py`**

```python
# reachy_openai_realtime/audio/capture.py
# ABOUTME: Dedicated mic-capture thread with a bounded drop-oldest frame buffer,
# ABOUTME: plus the stall-recovery ladder (spec §6). Isolates blocking SDK calls.
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)


class AudioPipelineStalled(RuntimeError):
    """Mic recovery exhausted; the whole app session must be rebuilt."""


class CaptureWorker:
    """Continuously drains media.get_audio_sample() so the SDK buffer never
    grows unbounded (reachy_mini issue #436), regardless of session state."""

    def __init__(self, media: Any, *, max_buffer_ms: float = 500.0) -> None:
        self._media = media
        self._max_buffer_ms = max_buffer_ms
        self._frames: deque[np.ndarray] = deque()
        self._buffered_ms = 0.0
        self._lock = threading.Lock()
        self._available = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_rate = 16_000
        self.last_frame_at = time.monotonic()
        self.frames_total = 0
        self.dropped_frames = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._available.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def frame_age_seconds(self) -> float:
        return time.monotonic() - self.last_frame_at

    def pop(self, timeout_seconds: float) -> np.ndarray | None:
        if not self._available.wait(timeout_seconds):
            return None
        with self._lock:
            if not self._frames:
                self._available.clear()
                return None
            frame = self._frames.popleft()
            self._buffered_ms -= self._frame_ms(frame)
            if not self._frames:
                self._available.clear()
            return frame

    def _run(self) -> None:
        try:
            self._sample_rate = int(self._media.get_input_audio_samplerate())
        except Exception:
            logger.exception("could not read input samplerate; assuming 16 kHz")
        while not self._stop.is_set():
            try:
                frame = self._media.get_audio_sample()
            except Exception:
                logger.exception("get_audio_sample failed")
                time.sleep(0.1)
                continue
            if frame is None or len(frame) == 0:
                time.sleep(0.005)  # SDK example polling cadence
                continue
            self.last_frame_at = time.monotonic()
            self.frames_total += 1
            with self._lock:
                self._frames.append(frame)
                self._buffered_ms += self._frame_ms(frame)
                while self._buffered_ms > self._max_buffer_ms and len(self._frames) > 1:
                    dropped = self._frames.popleft()
                    self._buffered_ms -= self._frame_ms(dropped)
                    self.dropped_frames += 1
                self._available.set()

    def _frame_ms(self, frame: np.ndarray) -> float:
        return len(frame) / self._sample_rate * 1000.0


class AudioRecoveryLadder:
    """Pure escalation logic for mic stalls: restart capture → restart media
    pipeline → restart app session. Never reboots Reachy (spec §6)."""

    ACTIONS = ("restart_capture", "restart_media", "restart_session")

    def __init__(
        self,
        *,
        stall_seconds: float = 1.75,
        cooldown_seconds: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stall_seconds = stall_seconds
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._attempt = 0
        self._last_action_at: float | None = None

    def next_action(self, frame_age_seconds: float) -> str | None:
        if frame_age_seconds < self._stall_seconds:
            self._attempt = 0
            self._last_action_at = None
            return None
        now = self._clock()
        if self._last_action_at is not None and now - self._last_action_at < self._cooldown_seconds:
            return None
        action = self.ACTIONS[min(self._attempt, len(self.ACTIONS) - 1)]
        self._attempt += 1
        self._last_action_at = now
        return action
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_audio_capture.py -v`
Expected: all PASS

- [ ] **Step 7: Integrate into `realtime.py`.**
  - `__init__`: `self._mic_ladder = AudioRecoveryLadder()`; `self._capture: CaptureWorker | None = None`.
  - Top of `run()` (before the reconnect loop): `self._capture = CaptureWorker(self.robot.media); self._capture.start()`; wrap the whole loop in `try/finally` with `self._capture.close()` in the finally. The worker outlives reconnects on purpose — reconnecting must not touch the mic.
  - In `run()`'s exception handling (Task 7 shape), add before the generic `except Exception`:

```python
except AudioPipelineStalled:
    raise  # escalation: main.py rebuilds the entire app session (mic ladder attempt 3)
```

  - `_record_loop`: replace the per-frame `await asyncio.to_thread(self.robot.media.get_audio_sample)` and its `None → break` with:

```python
frame = await asyncio.to_thread(self._capture.pop, 0.25)
if frame is None:
    action = self._mic_ladder.next_action(self._capture.frame_age_seconds())
    if action == "restart_capture":
        self.status.record_event("audio.capture.stalled", action=action)
        await asyncio.to_thread(self._restart_capture)
        self.status.record_event("audio.capture.restarted", action=action)
    elif action == "restart_media":
        self.status.record_event("audio.capture.stalled", action=action)
        await asyncio.to_thread(self._restart_media_pipeline)
        self.status.record_event("audio.capture.restarted", action=action)
    elif action == "restart_session":
        self.status.record_event("audio.capture.stalled", action=action)
        raise AudioPipelineStalled("microphone frames stopped; capture and media restarts failed")
    continue
```

  - Add the two sync restart helpers (called via `to_thread`; ordering respects the shared Wireless pipeline — recording is asserted before playback):

```python
def _restart_capture(self) -> None:
    self.robot.media.stop_recording()
    self.robot.media.start_recording()

def _restart_media_pipeline(self) -> None:
    self.robot.media.stop_playing()
    self.robot.media.stop_recording()
    self.robot.media.start_recording()
    self.robot.media.start_playing()
```

- [ ] **Step 8: Escalation in `main.py`.** In the outer run loop's exception handling, catch `AudioPipelineStalled` before the generic handler; re-assert media before the next session:

```python
except AudioPipelineStalled:
    self.status.add_event("warning", "audio pipeline stalled; restarting app session")
    try:
        reachy_mini.media.stop_playing()
        reachy_mini.media.stop_recording()
        reachy_mini.media.start_recording()
        reachy_mini.media.start_playing()
    except Exception:
        logger.exception("media re-init after stall failed")
    continue
```

(Use the actual robot variable name in `main.py`'s run loop — read it; do not reboot or exit.)

- [ ] **Step 9: Update session tests for the worker.** Record-loop tests no longer exit on `get_audio_sample() → None` (the worker absorbs it) — they exit via `stop_event`:
  - In `tests/test_realtime_fsm.py` and `tests/test_realtime_manual_turn.py`, every session running `_record_loop` gains:

```python
session._capture = CaptureWorker(session.robot.media, max_buffer_ms=60_000.0)
session._mic_ladder = AudioRecoveryLadder()
session._capture.start()
```

  before `asyncio.run(...)` and `session._capture.close()` after (huge buffer: the worker drains the fake instantly; the test must not drop frames the VAD needs).
  - `test_frames_ignored_while_waiting_for_response` needs a stop trigger since no `response.create` fires — replace its `FakeMedia(frames)` with:

```python
class ExhaustionStopMedia(FakeMedia):
    def __init__(self, frames, stop_event) -> None:
        super().__init__(frames)
        self._stop_event = stop_event

    def get_audio_sample(self):
        frame = super().get_audio_sample()
        if frame is None:
            self._stop_event.stopped = True
        return frame
```

  (add it to `tests/conftest.py` importing FakeMedia is circular — define it inside `test_realtime_fsm.py`).
  - `test_doa_poller_never_blocks_caller_when_usb_read_stalls` is untouched.

- [ ] **Step 10: Run the full suite, lint, commit**

```bash
uv run pytest -v && uv run ruff check .
git add reachy_openai_realtime/audio reachy_openai_realtime/realtime.py reachy_openai_realtime/main.py tests/test_audio_capture.py tests/test_realtime_fsm.py tests/test_realtime_manual_turn.py
git commit -m "feat: dedicated mic capture worker with stall-recovery ladder"
```

---

### Task 10: Latency-bounded playback buffer + speaker worker

**Files:**
- Create: `reachy_openai_realtime/audio/playback.py`
- Modify: `reachy_openai_realtime/realtime.py` (delta handler, playback loop, `_clear_playback`, `reset_connection_state`, `run()` worker lifecycle)
- Test: `tests/test_audio_playback.py`; update `tests/test_realtime_manual_turn.py`, `tests/test_realtime_reset.py`, `tests/test_realtime_fsm.py`

**Interfaces:**
- Consumes: epochs (Task 6), FSM (Task 5), watchdog `response_cancel` arming (Task 8), `RecentIds` (Task 6).
- Produces (from `reachy_openai_realtime.audio.playback`):
  - `@dataclass class PlaybackChunk: epoch: int; response_id: str; pcm: np.ndarray; duration_ms: float; received_at: float`
  - `@dataclass class PushResult: dropped_ms: float; overrun: bool`
  - `class PlaybackBuffer:`
    - `__init__(self, *, target_ms: float = 200.0, max_ms: float = 500.0, hard_max_ms: float = 1000.0) -> None`
    - `push(self, chunk: PlaybackChunk) -> PushResult` — over `max_ms` drops OLDEST until under; at/over `hard_max_ms` sets `overrun=True` (caller clears + cancels)
    - `pop_wait(self, timeout_seconds: float, current_epoch: int) -> PlaybackChunk | None` — blocking (call via `to_thread`); silently discards stale-epoch chunks
    - `queued_ms(self) -> float` / `clear(self) -> float` (returns dropped ms) — all methods thread-safe
  - `class SpeakerWorker:`
    - `__init__(self, media: Any, *, inbox_max: int = 4, on_write: Callable[[float, float], None] | None = None) -> None` — `on_write(duration_ms, received_at)` fires in the worker thread after each successful `push_audio_sample`
    - `start(self) -> None` / `close(self) -> None` (join timeout 2.0)
    - `submit(self, pcm: np.ndarray, duration_ms: float, received_at: float, timeout_seconds: float) -> bool` — False when the inbox stays full (speaker stall signal)
    - `flush(self) -> None` — drop queued inbox entries
    - attrs: `last_write_at: float`, `frames_total: int`; `stalled(self, threshold_seconds: float) -> bool`
- `RealtimeRobotSession._playback: PlaybackBuffer`, `RealtimeRobotSession._speaker: SpeakerWorker` (started/closed in `run()` alongside `_capture`); `RealtimeRobotSession._handle_playback_overrun(dropped_ms: float) -> None` (async)
- The old `RealtimeRobotSession._playback_queue: asyncio.Queue` is DELETED. Grep `rg -n "_playback_queue" reachy_openai_realtime tests` — every hit gets migrated in this task.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audio_playback.py
import time

import numpy as np

from reachy_openai_realtime.audio.playback import PlaybackBuffer, PlaybackChunk, SpeakerWorker


def chunk(ms: float, *, epoch: int = 1, response_id: str = "resp_1") -> PlaybackChunk:
    samples = int(24_000 * ms / 1000.0)
    return PlaybackChunk(
        epoch=epoch,
        response_id=response_id,
        pcm=np.zeros(samples, dtype=np.int16),
        duration_ms=ms,
        received_at=time.monotonic(),
    )


def test_push_pop_fifo_and_queued_ms_accounting() -> None:
    buffer = PlaybackBuffer()
    first = chunk(80.0)
    buffer.push(first)
    buffer.push(chunk(80.0))
    assert buffer.queued_ms() == 160.0
    assert buffer.pop_wait(0.1, current_epoch=1) is first
    assert buffer.queued_ms() == 80.0


def test_over_max_drops_oldest_until_under_limit() -> None:
    buffer = PlaybackBuffer(max_ms=500.0, hard_max_ms=10_000.0)
    results = [buffer.push(chunk(100.0)) for _ in range(7)]
    assert buffer.queued_ms() <= 500.0
    assert sum(result.dropped_ms for result in results) >= 200.0
    assert not any(result.overrun for result in results)


def test_hard_max_signals_overrun() -> None:
    buffer = PlaybackBuffer(max_ms=5_000.0, hard_max_ms=1_000.0)
    results = [buffer.push(chunk(200.0)) for _ in range(6)]
    assert results[-1].overrun is True


def test_pop_wait_skips_stale_epochs() -> None:
    buffer = PlaybackBuffer()
    buffer.push(chunk(100.0, epoch=1))
    buffer.push(chunk(100.0, epoch=2))
    popped = buffer.pop_wait(0.1, current_epoch=2)
    assert popped is not None
    assert popped.epoch == 2
    assert buffer.pop_wait(0.05, current_epoch=2) is None


def test_pop_wait_times_out_when_empty() -> None:
    buffer = PlaybackBuffer()
    started = time.monotonic()
    assert buffer.pop_wait(0.1, current_epoch=1) is None
    assert time.monotonic() - started < 1.0


def test_clear_returns_dropped_ms() -> None:
    buffer = PlaybackBuffer()
    buffer.push(chunk(150.0))
    buffer.push(chunk(150.0))
    assert buffer.clear() == 300.0
    assert buffer.queued_ms() == 0.0


class FakeSpeakerMedia:
    def __init__(self) -> None:
        self.pushed: list[np.ndarray] = []

    def push_audio_sample(self, data: np.ndarray) -> None:
        self.pushed.append(data)


def test_speaker_worker_writes_in_order_and_reports_writes() -> None:
    media = FakeSpeakerMedia()
    writes: list[float] = []
    worker = SpeakerWorker(media, on_write=lambda duration_ms, received_at: writes.append(duration_ms))
    worker.start()
    try:
        pcm = np.zeros((480, 2), dtype=np.float32)
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=1.0) is True
        deadline = time.monotonic() + 2.0
        while not media.pushed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(media.pushed) == 1
        assert writes == [20.0]
        assert worker.frames_total == 1
        assert worker.stalled(threshold_seconds=5.0) is False
    finally:
        worker.close()


def test_speaker_worker_submit_false_when_inbox_wedged() -> None:
    class WedgedMedia:
        def push_audio_sample(self, data: np.ndarray) -> None:
            time.sleep(10.0)

    worker = SpeakerWorker(WedgedMedia(), inbox_max=1)
    worker.start()
    try:
        pcm = np.zeros((480, 2), dtype=np.float32)
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.2) is True
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.2) is True  # queued
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.2) is False  # wedged
    finally:
        worker.close()


def test_speaker_worker_flush_drops_queued_audio() -> None:
    media = FakeSpeakerMedia()
    worker = SpeakerWorker(media, inbox_max=4)
    pcm = np.zeros((480, 2), dtype=np.float32)
    worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.1)  # worker not started: stays queued
    worker.flush()
    worker.start()
    try:
        time.sleep(0.1)
        assert media.pushed == []
    finally:
        worker.close()
```

(`SpeakerWorker.close()` with a wedged media thread: the thread is a daemon and `join(timeout=2.0)` gives up — the test tolerates the orphan because the process exits; this mirrors production where a wedged ALSA write can only be abandoned. `submit` returning False is the recovery signal.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_audio_playback.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `audio/playback.py`**

```python
# reachy_openai_realtime/audio/playback.py
# ABOUTME: Latency-bounded playback jitter buffer (spec §7) and the dedicated
# ABOUTME: speaker-write thread. Freshness beats completeness: oldest audio drops first.
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)

TARGET_BUFFER_MS = 200.0
MAX_BUFFER_MS = 500.0
HARD_MAX_BUFFER_MS = 1000.0


@dataclass
class PlaybackChunk:
    epoch: int
    response_id: str
    pcm: np.ndarray
    duration_ms: float
    received_at: float


@dataclass
class PushResult:
    dropped_ms: float
    overrun: bool


class PlaybackBuffer:
    """Time-accounted FIFO. Thread-safe: the event loop pushes, a to_thread
    consumer pops, and the status API reads queued_ms."""

    def __init__(
        self,
        *,
        target_ms: float = TARGET_BUFFER_MS,
        max_ms: float = MAX_BUFFER_MS,
        hard_max_ms: float = HARD_MAX_BUFFER_MS,
    ) -> None:
        self.target_ms = target_ms
        self.max_ms = max_ms
        self.hard_max_ms = hard_max_ms
        self._chunks: deque[PlaybackChunk] = deque()
        self._queued_ms = 0.0
        self._lock = threading.Lock()
        self._available = threading.Event()

    def push(self, chunk: PlaybackChunk) -> PushResult:
        dropped_ms = 0.0
        with self._lock:
            self._chunks.append(chunk)
            self._queued_ms += chunk.duration_ms
            while self._queued_ms > self.max_ms and len(self._chunks) > 1:
                dropped = self._chunks.popleft()
                self._queued_ms -= dropped.duration_ms
                dropped_ms += dropped.duration_ms
            overrun = self._queued_ms >= self.hard_max_ms
            self._available.set()
        return PushResult(dropped_ms=dropped_ms, overrun=overrun)

    def pop_wait(self, timeout_seconds: float, current_epoch: int) -> PlaybackChunk | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._available.wait(remaining):
                return None
            with self._lock:
                while self._chunks:
                    chunk = self._chunks.popleft()
                    self._queued_ms -= chunk.duration_ms
                    if chunk.epoch != current_epoch:
                        continue  # stale connection audio must never play (spec §4)
                    if not self._chunks:
                        self._available.clear()
                    return chunk
                self._queued_ms = 0.0
                self._available.clear()

    def queued_ms(self) -> float:
        with self._lock:
            return self._queued_ms

    def clear(self) -> float:
        with self._lock:
            dropped = self._queued_ms
            self._chunks.clear()
            self._queued_ms = 0.0
            self._available.clear()
            return dropped


class SpeakerWorker:
    """Owns all push_audio_sample calls so a wedged ALSA write can never block
    the event loop. Never touches stop_playing (shared Wireless pipeline)."""

    def __init__(
        self,
        media: Any,
        *,
        inbox_max: int = 4,
        on_write: Callable[[float, float], None] | None = None,
    ) -> None:
        self._media = media
        self._inbox: queue.Queue[tuple[np.ndarray, float, float]] = queue.Queue(maxsize=inbox_max)
        self._on_write = on_write
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_write_at = time.monotonic()
        self.frames_total = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-speaker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def submit(self, pcm: np.ndarray, duration_ms: float, received_at: float, timeout_seconds: float) -> bool:
        try:
            self._inbox.put((pcm, duration_ms, received_at), timeout=timeout_seconds)
            return True
        except queue.Full:
            return False

    def flush(self) -> None:
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                return

    def stalled(self, threshold_seconds: float) -> bool:
        return self._inbox.full() and time.monotonic() - self.last_write_at > threshold_seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                pcm, duration_ms, received_at = self._inbox.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._media.push_audio_sample(pcm)
            except Exception:
                logger.exception("speaker write failed")
                continue
            self.last_write_at = time.monotonic()
            self.frames_total += 1
            if self._on_write is not None:
                try:
                    self._on_write(duration_ms, received_at)
                except Exception:
                    logger.exception("on_write callback failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_audio_playback.py -v`
Expected: all PASS

- [ ] **Step 5: Integrate into `realtime.py`.**
  - `__init__`: replace `self._playback_queue = asyncio.Queue(maxsize=64)` with `self._playback = PlaybackBuffer()` and add `self._speaker = SpeakerWorker(self.robot.media, on_write=self._on_speaker_write)` plus:

```python
def _on_speaker_write(self, duration_ms: float, received_at: float) -> None:
    self._last_speaker_write_at = time.monotonic()  # Task 11 adds latency metrics here
```

  - `run()`: `self._speaker.start()` next to `self._capture.start()`; `self._speaker.close()` in the same `finally`.
  - Delta handler (`response.output_audio.delta`): decode as today, then:

```python
result = self._playback.push(
    PlaybackChunk(
        epoch=self.connection_epoch,
        response_id=self._current_response_id or "",
        pcm=pcm,
        duration_ms=duration_ms,
        received_at=time.monotonic(),
    )
)
if result.overrun:
    await self._handle_playback_overrun(self._playback.clear())
```

    keeping the existing `_playback_pushed_ms`/`_speaker_busy_until` bookkeeping lines unchanged (they drive truncation math and drain detection). The old `put_nowait`/`QueueFull` drop-newest branch is deleted — drop-oldest now lives in the buffer, matching spec §7.
  - Playback loop: replace the queue-get body with:

```python
chunk = await asyncio.to_thread(self._playback.pop_wait, 0.25, self.connection_epoch)
if chunk is None:
    continue
pcm_out = self._prepare_output(chunk.pcm)  # the existing convert/resample/stereo lines, unchanged
accepted = await asyncio.to_thread(
    self._speaker.submit, pcm_out, chunk.duration_ms, chunk.received_at, 1.0
)
if not accepted and self._speaker.stalled(2.0):
    self.status.record_event("audio.playback.restarted", reason="speaker_stalled")
    self._speaker.flush()
    await asyncio.to_thread(self._restart_media_pipeline)
```

    (`_prepare_output` is whatever the current loop does between get and push — keep that code, hoist it into a small method if it isn't one already; `_restart_media_pipeline` is Task 9's helper.)
  - Add the overrun handler:

```python
async def _handle_playback_overrun(self, dropped_ms: float) -> None:
    self.status.record_event("audio.playback.overrun", dropped_ms=round(dropped_ms, 1))
    self.status.add_event("warning", "playback overran; dropping stale audio")
    response_id = self._current_response_id
    if response_id and self.connection is not None:
        self._interrupted_response_ids.add(response_id)
        try:
            await self.connection.response.cancel(response_id=response_id)
            self.watchdog.arm("response_cancel")
        except Exception:
            logger.exception("response.cancel after overrun failed")
    self._clear_playback()
    self.fsm.transition(SessionState.LISTENING, reason="playback_overrun")
```

  - `_clear_playback`: replace the queue-drain lines with `self._playback.clear()` and add `self._speaker.flush()` before the existing `clear_player()` + `start_recording()` lines. **Do not remove those two lines or their comment — the shared-pipeline gotcha stands.**
  - `reset_connection_state` (Task 6): replace the `_playback_queue` drain block with `self._playback.clear()` and `self._speaker.flush()`.

- [ ] **Step 6: Migrate remaining references.**

```bash
rg -n "_playback_queue" reachy_openai_realtime tests
```

Update every test hit: `session._playback_queue = asyncio.Queue()` → `session._playback = PlaybackBuffer()` (import from `reachy_openai_realtime.audio.playback`), plus `session._speaker = SpeakerWorker(FakeSpeakerMedia())` where `_clear_playback` runs (the barge-in tests). In `tests/test_realtime_reset.py`, the dirty-session setup pushes a chunk (`session._playback.push(chunk(100.0))` — reuse the local `chunk` helper pattern) and the assertion becomes `assert session._playback.queued_ms() == 0.0`.

- [ ] **Step 7: Add the overrun integration test** (append to `tests/test_audio_playback.py`):

```python
def test_playback_overrun_cancels_response_and_returns_to_listening() -> None:
    import asyncio

    from conftest import drive_fsm

    from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
    from reachy_openai_realtime.runtime_status import RuntimeStatus
    from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
    from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
    from test_realtime_manual_turn import BargeInMedia, BargeInMotion, FakeConnection, FakeStopEvent

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
    session._current_response_id = "resp_overrun"
    session._interrupted_response_ids = RecentIds()

    asyncio.run(session._handle_playback_overrun(900.0))

    assert session.connection.response.cancelled == ["resp_overrun"]
    assert "resp_overrun" in session._interrupted_response_ids
    assert session.fsm.state is SessionState.LISTENING
    assert session.robot.media.audio.cleared == 1
    assert session.robot.media.recording_restarts == 1
```

(Adjust the `_clear_playback` collaborator attributes to whatever that method actually touches — read it; `BargeInMedia` already models the clear/restart pair.)

- [ ] **Step 8: Run the full suite, lint, commit**

```bash
uv run pytest -v && uv run ruff check .
git add reachy_openai_realtime/audio/playback.py reachy_openai_realtime/realtime.py tests/test_audio_playback.py tests/test_realtime_manual_turn.py tests/test_realtime_reset.py tests/test_realtime_fsm.py
git commit -m "feat: latency-bounded playback buffer with speaker worker and overrun recovery"
```

---

### Task 11: Latency and reliability metrics wiring

**Files:**
- Modify: `reachy_openai_realtime/realtime.py` (timestamps + all `status.metrics.*` calls; `DoAPoller.age_seconds()`)
- Verify: `reachy_openai_realtime/main.py` (`/api/diagnostics` already embeds the runtime snapshot; see Step 3 — no change expected)
- Test: `tests/test_realtime_metrics.py`

**Interfaces:**
- Consumes: `MetricsRegistry` via `self.status.metrics` (Task 3), speaker `on_write` (Task 10), FSM/interrupt flow (Task 5), workers (Tasks 9–10).
- Produces:
  - `RealtimeRobotSession._speech_ended_at: float | None` — set to `time.monotonic()` when the turn commits (right after `input_audio_buffer.commit()`); cleared on reset.
  - `RealtimeRobotSession._observe_speech_latency(name: str) -> None` — records `(now - _speech_ended_at) * 1000` into `name` when the timestamp is set.
  - `RealtimeRobotSession._barge_in_at: float | None` — set at `_interrupt_assistant` entry.
  - `DoAPoller.age_seconds() -> float` — seconds since the last successful DoA read (`float("inf")` before the first).
  - Metric names (exact, spec §19): `speech_end_to_response_created_ms`, `speech_end_to_first_audio_received_ms`, `speech_end_to_first_audio_played_ms`, `audio_receive_to_playback_ms`, `barge_in_to_cancel_ms`, `barge_in_to_silence_ms`, `tool_duration_ms`; gauges `queued_audio_ms`, `mic_frame_age_ms`, `doa_age_ms`, `connection_uptime_seconds`; counters `reconnect_count`, `mic_restart_count`, `speaker_restart_count`, `tool_error_count`, `playback_overrun_count`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_realtime_metrics.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_realtime_metrics.py -v`
Expected: FAIL (`AttributeError: _observe_speech_latency` etc.)

- [ ] **Step 3: Implement in `realtime.py`.**
  - `__init__`: `self._speech_ended_at: float | None = None`, `self._barge_in_at: float | None = None`, `self._first_write_pending = False`.
  - Helper:

```python
def _observe_speech_latency(self, name: str) -> None:
    if self._speech_ended_at is None:
        return
    self.status.metrics.observe_ms(name, (time.monotonic() - self._speech_ended_at) * 1000.0)
```

  - Wire the sites:
    - `_record_loop` commit path: `self._speech_ended_at = time.monotonic()` immediately after `commit()`.
    - `response.created` handler: `self._observe_speech_latency("speech_end_to_response_created_ms")`; set `self._first_write_pending = True`.
    - first `response.output_audio.delta` of a response (the existing "first delta" branch that stamps `_playback_started_at`): `self._observe_speech_latency("speech_end_to_first_audio_received_ms")` and `self.status.record_event("response.first_audio_received", response_id=...)`.
    - `_on_speaker_write(duration_ms, received_at)` (extend Task 10's stub):

```python
def _on_speaker_write(self, duration_ms: float, received_at: float) -> None:
    now = time.monotonic()
    self.status.metrics.observe_ms("audio_receive_to_playback_ms", (now - received_at) * 1000.0)
    if self._first_write_pending:
        self._first_write_pending = False
        self._observe_speech_latency("speech_end_to_first_audio_played_ms")
        self.status.record_event("response.first_audio_played")
```

      (runs in the speaker thread — `MetricsRegistry` is thread-safe and the bool flip is GIL-atomic; the recorder is thread-safe too.)
    - `_interrupt_assistant`: `self._barge_in_at = time.monotonic()` first line; after `response.cancel` returns: `observe_ms("barge_in_to_cancel_ms", ...)`; after `_clear_playback()` completes: `observe_ms("barge_in_to_silence_ms", ...)` — both computed from `_barge_in_at`, guarded `if self._barge_in_at is not None`.
    - `_record_loop` per-iteration gauges: `set_gauge("mic_frame_age_ms", self._capture.frame_age_seconds() * 1000.0)`, `set_gauge("queued_audio_ms", self._playback.queued_ms())`, `set_gauge("doa_age_ms", ...)` from `DoAPoller.age_seconds()` (add that method: stamp `self._updated_at = time.monotonic()` on each successful read; return `float("inf")` → clamp gauge to `-1.0` when never read), `set_gauge("connection_uptime_seconds", now - self._connected_at)` (stamp `self._connected_at` in `_run_connection` after connect succeeds).
    - counters: `increment("reconnect_count")` in `run()`'s retry path (next to `realtime.reconnect` event); `increment("mic_restart_count")` at Task 9's `restart_capture`/`restart_media` action sites; `increment("speaker_restart_count")` at Task 10's speaker-stall restart site; `increment("playback_overrun_count")` in `_handle_playback_overrun`; `increment("tool_error_count")` in `_handle_tool_call`'s error/`ok: false` path; `observe_ms("tool_duration_ms", ...)` around the `motion.submit(...)` call in `_handle_tool_call` (Phase 1 note: this measures validate+enqueue only; the Phase 3 ToolExecutor will measure true execution).
  - `/api/diagnostics` needs no wiring: it already embeds `"runtime": self.runtime_status.snapshot()` (main.py:102), and Task 3 put `"metrics"` inside `snapshot()`. Verify with a quick assertion in the test file that `RuntimeStatus().snapshot()["metrics"]` has the `latency/counters/gauges` keys; only touch `main.py` if that embed has been removed.

- [ ] **Step 4: Run the full suite, lint, commit**

```bash
uv run pytest -v && uv run ruff check .
git add reachy_openai_realtime/realtime.py reachy_openai_realtime/main.py tests/test_realtime_metrics.py
git commit -m "feat: record speech, barge-in, and audio-pipeline latency metrics"
```

---

### Task 12: Scripted-connection chaos tests and leak checks

**Files:**
- Modify: `tests/conftest.py` (scripted Realtime transport)
- Test: `tests/test_chaos_reconnect.py`, `tests/test_chaos_protocol.py`

**Interfaces:**
- Consumes: everything Tasks 1–11 built.
- Produces (in `tests/conftest.py`, for this task and all later phases):
  - `realtime_event(type_: str, **attrs) -> SimpleNamespace`
  - `class ScriptedConnection:` — async-iterable fake with `session` (records `update` calls), `input_audio_buffer`, `response`, `conversation` recorders; `__init__(self, events: list, *, raise_after: Exception | None = None, on_drained: Callable[[], None] | None = None)`. Iteration yields each scripted event (with an `await asyncio.sleep(0)` between), then calls `on_drained` if set, then raises `raise_after` if set, else idles in `await asyncio.sleep(0.05)` forever.
  - `class FakeRealtimeClient:` — `FakeRealtimeClient(connections: list[ScriptedConnection])`; `client.realtime.connect(model=...)` returns an async context manager yielding the next scripted connection (raises `AssertionError` if exhausted).

- [ ] **Step 1: Extend `tests/conftest.py`**

```python
import asyncio
from types import SimpleNamespace
from typing import Callable


def realtime_event(type_: str, **attrs) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **attrs)


class _RecorderSession:
    def __init__(self) -> None:
        self.updates: list = []

    async def update(self, *, session) -> None:
        self.updates.append(session)


class _RecorderInputBuffer:
    def __init__(self) -> None:
        self.appended = 0
        self.committed = 0

    async def append(self, *, audio: str) -> None:
        self.appended += 1

    async def commit(self) -> None:
        self.committed += 1


class _RecorderResponse:
    def __init__(self) -> None:
        self.created: list = []
        self.cancelled: list[str | None] = []

    async def create(self, response=None) -> None:
        self.created.append(response)

    async def cancel(self, response_id: str | None = None) -> None:
        self.cancelled.append(response_id)


class _RecorderConversationItem:
    def __init__(self) -> None:
        self.created: list = []
        self.deleted: list[str] = []
        self.truncations: list = []

    async def create(self, **kwargs) -> None:
        self.created.append(kwargs)

    async def delete(self, *, item_id: str, **kwargs) -> None:
        self.deleted.append(item_id)

    async def truncate(self, **kwargs) -> None:
        self.truncations.append(kwargs)


class ScriptedConnection:
    """Fake Realtime connection: replays scripted server events, then fails or idles."""

    def __init__(
        self,
        events: list,
        *,
        raise_after: Exception | None = None,
        on_drained: Callable[[], None] | None = None,
    ) -> None:
        self._events = list(events)
        self._raise_after = raise_after
        self._on_drained = on_drained
        self.session = _RecorderSession()
        self.input_audio_buffer = _RecorderInputBuffer()
        self.response = _RecorderResponse()
        self.conversation = SimpleNamespace(item=_RecorderConversationItem())

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            await asyncio.sleep(0)
            yield event
        if self._on_drained is not None:
            self._on_drained()
        if self._raise_after is not None:
            raise self._raise_after
        while True:
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        pass


class FakeRealtimeClient:
    def __init__(self, connections: list[ScriptedConnection]) -> None:
        self._connections = list(connections)
        self.realtime = self

    def connect(self, *, model: str):
        assert self._connections, "scripted connections exhausted"
        connection = self._connections.pop(0)

        class _Ctx:
            async def __aenter__(_self):
                return connection

            async def __aexit__(_self, *exc_info):
                return False

        return _Ctx()
```

Check the shape `_run_connection` actually consumes (`async with self.client.realtime.connect(model=...)` then `async for event in connection` — confirm attribute names against `realtime.py` and adjust the fake to match exactly, including `connection.session.update`).

- [ ] **Step 2: Write the chaos tests.** `tests/test_chaos_reconnect.py`:

```python
import asyncio
import os
import threading

from conftest import FakeRealtimeClient, ScriptedConnection, realtime_event

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.recovery import SessionOutcome
from test_realtime_manual_turn import BargeInMotion, FakeMedia, stereo_frame


class ChaosMedia(FakeMedia):
    """FakeMedia plus the pipeline-restart surface the mic ladder may touch."""

    def __init__(self, frames) -> None:
        super().__init__(frames)
        self.recording_restarts = 0

    def stop_recording(self) -> None:
        pass

    def start_recording(self) -> None:
        self.recording_restarts += 1

    def stop_playing(self) -> None:
        pass

    def start_playing(self) -> None:
        pass


async def _instant_sleep(stop_event, seconds: float) -> None:
    await asyncio.sleep(0)


def build_session(connections: list[ScriptedConnection]) -> RealtimeRobotSession:
    """Full session through the REAL constructor (chaos tests exercise real wiring).
    __init__ signature (realtime.py:101): (robot, motion, config, status,
    language_provider=None, camera_enabled=None, capture_camera_jpeg=None); it
    builds a real AsyncOpenAI client internally, which demands an API key env
    var — hence the setdefault. The fake client is swapped in afterwards."""
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-chaos-key-0000000000")
    robot = type("Robot", (), {"media": ChaosMedia([stereo_frame(-60.0) for _ in range(5)])})()
    session = RealtimeRobotSession(robot, BargeInMotion(), AppConfig(), RuntimeStatus())
    session.client = FakeRealtimeClient(connections)
    return session


def test_disconnect_while_listening_reconnects_with_fresh_epoch() -> None:
    stop_event = threading.Event()
    session_updated = realtime_event("session.updated", session=None)
    first = ScriptedConnection([session_updated], raise_after=ConnectionError("wifi died"))
    # The second connection must ALSO end by raising: an idling ScriptedConnection
    # parks _event_loop in its `async for` forever and the gather never returns.
    # on_drained sets stop first, so run() sees the stop at the top of its retry
    # loop and exits STOPPED instead of scheduling a third attempt.
    second = ScriptedConnection(
        [session_updated], on_drained=stop_event.set, raise_after=ConnectionError("server closed")
    )
    session = build_session([first, second])
    session._sleep_unless_stopped = _instant_sleep  # collapse backoff delay

    outcome = asyncio.run(session.run(stop_event))

    assert outcome is SessionOutcome.STOPPED
    assert session.connection_epoch == 2
    assert session._playback.queued_ms() == 0.0
    counters = session.status.metrics.snapshot()["counters"]
    assert counters["reconnect_count"] == 1  # attempt 2 was a reconnect; stop pre-empts attempt 3


def test_ten_transient_failures_do_not_leak_threads_or_state() -> None:
    stop_event = threading.Event()
    attempts: list[int] = []
    session = build_session([])  # _run_connection is stubbed; connect() is never reached

    async def failing_run_connection(stop) -> None:
        attempts.append(session.connection_epoch)
        if len(attempts) >= 10:
            stop_event.set()
        raise ConnectionError("flaky network")

    session._run_connection = failing_run_connection  # type: ignore[method-assign]
    session._sleep_unless_stopped = _instant_sleep

    thread_count_before = threading.active_count()
    outcome = asyncio.run(session.run(stop_event))

    assert outcome is SessionOutcome.STOPPED
    assert attempts == list(range(1, 11))
    # capture/speaker workers started once and closed; no per-cycle thread growth
    assert threading.active_count() <= thread_count_before
    assert len(session._interrupted_response_ids) == 0
    assert session._playback.queued_ms() == 0.0


def test_interrupted_ids_stay_bounded_across_many_interrupts() -> None:
    ids = RecentIds(max_size=32)
    for index in range(500):
        ids.add(f"resp_{index}")
    assert len(ids) == 32
```

`tests/test_chaos_protocol.py`:

```python
import asyncio
import time
from types import SimpleNamespace

import pytest

from conftest import ScriptedConnection, drive_fsm, realtime_event

from reachy_openai_realtime.audio.playback import PlaybackBuffer
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog, WatchdogTimeout
from reachy_openai_realtime.vad import EnergyTurnDetector
from test_realtime_manual_turn import BargeInMotion, FakeStopEvent


def test_duplicate_response_done_flushes_tool_outputs_once() -> None:
    done = realtime_event(
        "response.done",
        response=SimpleNamespace(id="resp_dup", status="completed", usage=None, output=[]),
    )
    connection = ScriptedConnection([done, done])
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    # Attributes below mirror what the response.done path reads (realtime.py:708-756
    # pre-rewrite): config/_language_provider feed _flush_tool_outputs' instructions,
    # motion/_vad/_speaker_busy_until feed the no-more-audio branch. If the rewrite
    # added a read this misses, pytest's AttributeError names it — add it with the
    # neutral value used in tests/test_realtime_reset.py's dirty-session builder.
    session.status = RuntimeStatus()
    session.connection = connection
    session.config = AppConfig()
    session._language_provider = None
    session.motion = BargeInMotion()
    session._vad = EnergyTurnDetector()
    session.fsm = SessionStateMachine()
    drive_fsm(session.fsm, SessionState.WAITING_RESPONSE)
    session.watchdog = DeadlineWatchdog()
    session.connection_epoch = 1
    session._response_generation_done = False
    session._current_response_id = "resp_dup"
    session._interrupted_response_ids = RecentIds()
    session._pending_tool_outputs = [(1, "call_1", '{"ok": true}')]
    session._playback = PlaybackBuffer()
    session._speaker_busy_until = time.monotonic() - 1.0
    session._current_audio_item_id = None
    session._current_audio_content_index = 0

    async def run_event_loop() -> None:
        # After both scripted events the connection idles forever; cancel it.
        task = asyncio.ensure_future(session._event_loop(FakeStopEvent()))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_event_loop())

    tool_items = [
        kwargs
        for kwargs in connection.conversation.item.created
        if kwargs.get("item", {}).get("type") == "function_call_output"
    ]
    assert len(tool_items) == 1
    assert len(connection.response.created) == 1


def test_session_updated_timeout_tears_down_connection() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.now = 10.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.status = RuntimeStatus()
    session.watchdog = DeadlineWatchdog(clock=clock)
    session.watchdog.arm("session_update")

    async def advance_clock() -> None:
        await asyncio.sleep(0.05)
        clock.now += 60.0

    async def run() -> None:
        advancer = asyncio.ensure_future(advance_clock())
        try:
            await session._watchdog_loop()
        finally:
            advancer.cancel()

    with pytest.raises(WatchdogTimeout) as excinfo:
        asyncio.run(run())
    assert excinfo.value.operation == "session_update"
```

Both files' hand-built sessions must mirror what the exercised code paths actually read — run each test, and when an `AttributeError` names a missing attribute, add it with the neutral value used in `tests/test_realtime_reset.py`'s dirty-session builder. That is expected calibration, not failure.

- [ ] **Step 3: Run the chaos tests**

Run: `uv run pytest tests/test_chaos_reconnect.py tests/test_chaos_protocol.py -v`
Expected: all PASS (iterate on fake shapes per the calibration note until green)

- [ ] **Step 4: Run the full suite, lint, commit**

```bash
uv run pytest -v && uv run ruff check .
git add tests/conftest.py tests/test_chaos_reconnect.py tests/test_chaos_protocol.py
git commit -m "test: add scripted-connection chaos tests and reconnect leak checks"
```

---

### Task 13: Documentation, gotchas, and final verification

**Files:**
- Modify: `README.md` (add "Reliability & recovery" section)
- Modify: `plan.md` (Phase 1 status + pointers)
- Create: `gotchas.md`
- Modify: `docs/superpowers/plans/2026-08-17-phase1-reliability-foundation.md` (tick remaining checkboxes)

- [ ] **Step 1: README section.** Append a `## Reliability & recovery` section covering, in prose (adapt to the README's existing tone/structure — read it first):
  - the session FSM states and that `events.jsonl` logs every transition;
  - reconnect policy: infinite retries with 1→30 s jittered backoff, reset after 60 s healthy; fatal config errors (bad key/model) stop retries and surface in the UI until settings change; Realtime's 60-minute session cap makes periodic reconnects normal;
  - watchdog deadlines (table of the 7 operations/timeouts from `DEFAULT_DEADLINES`);
  - mic recovery ladder (restart capture → restart media → restart app session; never reboots the robot) and the playback overrun rule (drop-oldest over 500 ms, cancel + relisten at 1 s);
  - observability: `~/.config/reachy-mini/apps/reachy_openai_realtime/{events.jsonl,application.log}`, metrics in `/api/status` and `/api/diagnostics`, and that logs never contain API keys or raw audio.

- [ ] **Step 2: Extend `gotchas.md`.** The file was seeded at planning time with the hardware/API traps — extend it, never regenerate it (other agents add their own entries). Append the implementation pointers that exist only now:

```markdown
- Mic drain lives in `reachy_openai_realtime/audio/capture.py` (`CaptureWorker`). The stall
  ladder escalates restart_capture → restart_media → restart_session and NEVER reboots the OS.
- Playback freshness: `audio/playback.py` drops oldest past 500 ms and cancels + relistens at
  1 s (`audio.playback.overrun` in events.jsonl). Don't "fix" audio gaps by buffering more.
- Fatal vs transient connection errors: `session/recovery.py:classify_connection_error` —
  429 is TRANSIENT and is checked BEFORE the 4xx→FATAL rule. Keep that ordering.
- Reconnect policy: infinite jittered backoff 1→30 s, reset after 60 s healthy; fatal config
  errors park in `config_error` until settings change (`main.py` fingerprint wait loop).
```

- [ ] **Step 3: Update `plan.md`.** Add a short status block: Phase 1 (reliability foundation) implemented per `docs/superpowers/plans/2026-08-17-phase1-reliability-foundation.md`; spec at `docs/production-hardening-spec.md`; Phases 2–6 pending, each gets its own plan.

- [ ] **Step 4: Full verification**

```bash
uv run ruff check . && uv run pytest -v
```

Expected: clean lint, all tests green, zero new warnings in output. Physical smoke test on the robot is not possible from the dev machine — say so in the completion report rather than claiming it; the Phase 6 soak covers it.

- [ ] **Step 5: Commit**

```bash
git add README.md plan.md gotchas.md docs/superpowers/plans/2026-08-17-phase1-reliability-foundation.md
git commit -m "docs: document Phase 1 reliability behavior and project gotchas"
```

---

## Spec-coverage map (Phase 1 scope)

| Spec section | Where |
|---|---|
| §2 latency-critical path untouched, freshness>completeness, independent recovery, epochs | Tasks 5–10 (no new services added anywhere) |
| §3 explicit FSM | Tasks 4–5 |
| §4 epochs + canonical reset | Task 6 (+ chunk tagging in Task 10) |
| §5 watchdogs, backoff, error classes | Tasks 7–8 |
| §6 capture worker, mic watchdog ladder, speaker worker | Tasks 9–10 |
| §7 latency-bounded playback (200/500/1000 ms) | Task 10 |
| §8 barge-in epoch checks, INTERRUPTING, cancel timeout, latency metrics | Tasks 5, 6, 8, 11 |
| §9 VAD | unchanged this phase (existing adaptive VAD retained; fallback hierarchy is later-phase work) |
| §18 events.jsonl + application.log + taxonomy + redaction | Tasks 1, 3 (+ event calls throughout) |
| §19 metrics + aggregates + /api/diagnostics | Tasks 2, 3, 11 |
| §26 unit/chaos rows for FSM/epochs/buffer/watchdog/backoff/redaction | Tasks 1–12 test steps |
| §27 leak checks, bounded interrupted-IDs | Tasks 6, 12 |
| §28 module layout (session/, audio/, observability/) | Tasks 1, 2, 4, 7, 8, 9, 10 |
| §29 Phase 1 items 1–9 | all of the above |
| §31 constraints | Global Constraints section; per-task commits |

Out of scope here by design: §10–§17, §20–§25 (Phases 2–5), §9 neural-VAD fallback, §24 full supervisor (Phase 1 ships its audio/watchdog subset).

