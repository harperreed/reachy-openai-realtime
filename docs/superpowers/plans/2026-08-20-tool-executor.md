# ToolExecutor Implementation Plan (issue #21)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move model tool-call execution off the Realtime receive loop into a bounded async `ToolExecutor` with per-tool timeouts, duplicate/stale-epoch guards, cancellation, and structured error results.

**Architecture:** A new flat module `reachy_openai_realtime/tool_executor.py` (sibling of `vad.py`/`dsp.py`/`usage.py`) owns a name→handler registry and runs each invocation in its own asyncio task behind a 2-slot semaphore. `realtime.py` submits invocations from the receive loop and gets results back through an async callback that does today's bookkeeping (events, metrics, `record_motion`, pending-output append) and flushes late-arriving outputs. Errors always become `{"ok": false, "error": ...}` JSON — nothing ever raises into the receive loop.

**Tech Stack:** Python 3.10+ stdlib asyncio only. No new dependencies. Tests use plain pytest with `asyncio.run()` (NO pytest-asyncio — do not add it).

**Spec:** GitHub issue #21 ("Phase 3: ToolExecutor — async tool dispatch off the receive loop"), which implements `docs/production-hardening-spec.md` §14. Read the issue with `gh issue view 21` before starting. The hardening spec is verbatim reference — never edit it.

## Global Constraints

Every task's requirements implicitly include all of these:

- Canonical check before every commit: `uv run ruff check . && uv run pytest` — both green, zero new warnings.
- No new dependencies, runtime or dev. Tests wrap async code in `asyncio.run()`; pytest-asyncio is deliberately absent.
- Never store, log, print, or return the OpenAI API key. The robots' `~/.config/reachy-mini/apps/reachy_openai_realtime/.env` files contain a real key — never read them. All redaction stays in `observability/events.py:redact_secrets`.
- Event vocabulary is fixed: this plan uses only the existing `tool.requested`, `tool.completed`, `tool.failed` names (guard drops are `tool.failed` with an `error` field — no new event names).
- NEVER call `ReachyMini.cancel_move()` or `media.stop_playing()` from app code. The four-line GStreamer comment in `motion/manager.py` `stop_current` must survive untouched.
- The Realtime model never gets direct unrestricted joint-angle control; no separate STT/LLM/TTS/memory/orchestration services enter normal turns.
- `docs/production-hardening-spec.md` stays verbatim — reference only.
- Every new source file starts with a two-line `# ABOUTME:` header. Ruff line-length is 110. Match surrounding style.
- Conventional commits, imperative present tense. `git add` names specific files only — never `git add -A`/`-u`.
- `reachy_openai_realtime/config.py` carries an intentional uncommitted owner edit (one line inside `session_instructions`). This plan never touches `config.py`; if you see it dirty in `git status`, leave it alone — never commit or revert it.
- Watchdog invariant introduced by this plan: the `tool_response` deadline armed at dispatch must exceed the dispatched tool's timeout (enforced by construction: `timeout_s + TOOL_WATCHDOG_GRACE_S`, tested in Task 4).

## Context an implementer needs

- `reachy_openai_realtime/realtime.py` is the 1200-line session driver. Tool calls arrive in the receive loop at `"response.function_call_arguments.done"` (line ~938) → `_handle_tool_call` (line ~1016), which today calls `self.motion.submit(name, arguments)` **synchronously inline** — a slow tool stalls all event processing. That inline call is what this plan replaces.
- `self.connection_epoch` increments per connection attempt; `_pending_tool_outputs` is a `list[tuple[int, str, str]]` of `(epoch, call_id, output_json)`; `_flush_tool_outputs` (line ~1038) skips stale epochs and sends `function_call_output` items followed by `response.create`.
- `DeadlineWatchdog` (`session/watchdog.py`): `arm(operation, timeout_seconds=None)` — `None` looks up `DEFAULT_DEADLINES`; an explicit float overrides it. `DEFAULT_DEADLINES["tool_response"]` is 5.0 and stays 5.0: the existing arm at the end of `_flush_tool_outputs` (watching for the model's follow-up response, disarmed on `response.created`) keeps using the default. The NEW arm at dispatch passes an explicit deadline of `tool timeout + 2s grace`, because #21's tool timeouts (10–15s) exceed the 5s default and a legal slow tool must not trip a reconnect.
- FSM legality (`session/fsm.py` `LEGAL_TRANSITIONS`): `TOOL_EXECUTION` is reachable from `WAITING_RESPONSE` and `ASSISTANT_SPEAKING`; `WAITING_RESPONSE` is reachable from `TOOL_EXECUTION` and `LISTENING`. The integration in Task 4 relies on exactly these edges — no FSM changes needed.
- House test pattern for session internals: `RealtimeRobotSession.__new__(RealtimeRobotSession)` bare sessions with only the fields the method under test touches (see `tests/test_chaos_protocol.py` for a working example that exercises `_flush_tool_outputs`).
- `parallel_tool_calls=False` is set in `_session_config`, so the model sends at most one tool call per response in practice. The executor still supports 2 concurrent slots per #21; the multi-in-flight edge cases below are defensive.

---

### Task 1: `tool_executor.py` — invocation, registry, happy-path dispatch

**Files:**
- Create: `reachy_openai_realtime/tool_executor.py`
- Modify: `reachy_openai_realtime/realtime.py` (delete the `RecentIds` class at lines ~55–79; import it from the new module instead)
- Test: `tests/test_tool_executor.py` (create)

**Interfaces:**
- Consumes: `RecentIds` moved verbatim from `realtime.py` (bounded recent-ID set: `RecentIds(max_size=32)`, `.add(id)`, `id in ids`, `.clear()`).
- Produces (later tasks and the memory plan rely on these exact names):
  - `ToolInvocation(epoch: int, call_id: str, name: str, arguments: dict)` — frozen dataclass.
  - `ToolExecutor(*, epoch_provider, on_output, record_event, max_parallel=MAX_PARALLEL_TOOLS)`
  - `ToolExecutor.register(name: str, handler, *, timeout_s: float = DEFAULT_TOOL_TIMEOUT_S, category: str = "other") -> None` — handler is `async (dict) -> dict`; re-registering a name overwrites.
  - `ToolExecutor.timeout_for(name: str) -> float` (default for unknown names)
  - `ToolExecutor.busy() -> bool`
  - `async ToolExecutor.submit(invocation: ToolInvocation) -> bool` (False = dropped by a guard)
  - `async ToolExecutor.cancel_all() -> None`
  - `on_output` callback signature: `async (invocation: ToolInvocation, result: dict, output_json: str, duration_ms: float) -> None`
  - Constants: `MAX_PARALLEL_TOOLS = 2`, `MAX_RESULT_CHARS = 16_384`, `DEFAULT_TOOL_TIMEOUT_S = 15.0`, `MOTION_TOOL_TIMEOUT_S = 10.0`, `CAMERA_TOOL_TIMEOUT_S = 5.0`, `TOOL_WATCHDOG_GRACE_S = 2.0`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tool_executor.py`:

```python
# ABOUTME: Tests for ToolExecutor (issue #21): registry, guards, timeouts,
# ABOUTME: bounded concurrency, cancellation, and structured error results.
import asyncio

from reachy_openai_realtime.tool_executor import (
    DEFAULT_TOOL_TIMEOUT_S,
    ToolExecutor,
    ToolInvocation,
)


class OutputRecorder:
    def __init__(self):
        self.outputs = []

    async def __call__(self, invocation, result, output, duration_ms):
        self.outputs.append((invocation, result, output, duration_ms))


class EventRecorder:
    def __init__(self):
        self.events = []

    def __call__(self, event, **fields):
        self.events.append((event, fields))


def make_executor(**kwargs):
    epoch = {"value": 1}
    outputs = OutputRecorder()
    events = EventRecorder()
    executor = ToolExecutor(
        epoch_provider=lambda: epoch["value"],
        on_output=outputs,
        record_event=events,
        **kwargs,
    )
    return executor, outputs, events, epoch


async def drain(executor, timeout_s=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while executor.busy():
        assert loop.time() < deadline, "executor did not drain"
        await asyncio.sleep(0.005)


def test_registered_tool_runs_and_delivers_output():
    async def scenario():
        executor, outputs, events, _ = make_executor()

        async def echo(arguments):
            return {"ok": True, "echo": arguments["x"]}

        executor.register("echo", echo, timeout_s=1.0)
        accepted = await executor.submit(ToolInvocation(1, "call_1", "echo", {"x": 7}))
        assert accepted is True
        await drain(executor)
        assert len(outputs.outputs) == 1
        invocation, result, output, duration_ms = outputs.outputs[0]
        assert invocation.call_id == "call_1"
        assert result == {"ok": True, "echo": 7}
        assert '"echo": 7' in output
        assert duration_ms >= 0.0

    asyncio.run(scenario())


def test_unknown_tool_returns_structured_error():
    async def scenario():
        executor, outputs, events, _ = make_executor()
        await executor.submit(ToolInvocation(1, "call_1", "nope", {}))
        await drain(executor)
        (_, result, _, _) = outputs.outputs[0]
        assert result["ok"] is False
        assert "unknown tool" in result["error"]

    asyncio.run(scenario())


def test_timeout_for_returns_registered_or_default():
    executor, _, _, _ = make_executor()

    async def noop(arguments):
        return {"ok": True}

    executor.register("slowpoke", noop, timeout_s=9.5)
    assert executor.timeout_for("slowpoke") == 9.5
    assert executor.timeout_for("missing") == DEFAULT_TOOL_TIMEOUT_S


def test_recent_ids_still_importable_from_realtime():
    from reachy_openai_realtime.realtime import RecentIds as FromRealtime
    from reachy_openai_realtime.tool_executor import RecentIds as FromExecutor

    assert FromRealtime is FromExecutor
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tool_executor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reachy_openai_realtime.tool_executor'`

- [ ] **Step 3: Write the module**

Create `reachy_openai_realtime/tool_executor.py`. Cut the `RecentIds` class (lines ~55–79) out of `realtime.py` verbatim and paste it here unchanged; then in `realtime.py` add `RecentIds` to a new import `from .tool_executor import RecentIds` (existing `from reachy_openai_realtime.realtime import RecentIds` in five test files keeps working because the name stays in `realtime`'s namespace).

```python
# ABOUTME: Async tool executor (issue #21): runs model tool calls off the Realtime
# ABOUTME: receive loop with per-tool timeouts, epoch/duplicate guards, bounded concurrency.
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAX_PARALLEL_TOOLS = 2
MAX_RESULT_CHARS = 16_384
DEFAULT_TOOL_TIMEOUT_S = 15.0
MOTION_TOOL_TIMEOUT_S = 10.0
CAMERA_TOOL_TIMEOUT_S = 5.0  # reserved per issue #21; no camera tool registered today
TOOL_WATCHDOG_GRACE_S = 2.0

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
OutputCallback = Callable[["ToolInvocation", dict[str, Any], str, float], Awaitable[None]]


# <RecentIds class pasted here verbatim from realtime.py — do not rewrite it>


@dataclass(frozen=True)
class ToolInvocation:
    epoch: int
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class RegisteredTool:
    handler: ToolHandler
    timeout_s: float
    category: str  # names the timeout error: f"{category}_timeout"


class ToolExecutor:
    """Bounded async dispatch for model-invoked tools (issue #21).

    Every outcome — success, timeout, handler exception, guard drop — is a
    structured dict; nothing ever raises into the submitting (receive) loop.
    """

    def __init__(
        self,
        *,
        epoch_provider: Callable[[], int],
        on_output: OutputCallback,
        record_event: Callable[..., None],
        max_parallel: int = MAX_PARALLEL_TOOLS,
    ) -> None:
        self._epoch_provider = epoch_provider
        self._on_output = on_output
        self._record_event = record_event
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._tools: dict[str, RegisteredTool] = {}
        self._seen_call_ids = RecentIds()
        self._tasks: set[asyncio.Task[None]] = set()

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
        category: str = "other",
    ) -> None:
        self._tools[name] = RegisteredTool(handler=handler, timeout_s=timeout_s, category=category)

    def timeout_for(self, name: str) -> float:
        registered = self._tools.get(name)
        return DEFAULT_TOOL_TIMEOUT_S if registered is None else registered.timeout_s

    def busy(self) -> bool:
        return bool(self._tasks)

    async def submit(self, invocation: ToolInvocation) -> bool:
        if invocation.epoch != self._epoch_provider():
            self._record_event(
                "tool.failed", name=invocation.name, call_id=invocation.call_id, error="stale_epoch"
            )
            return False
        if invocation.call_id in self._seen_call_ids:
            self._record_event(
                "tool.failed", name=invocation.name, call_id=invocation.call_id, error="duplicate_call_id"
            )
            return False
        self._seen_call_ids.add(invocation.call_id)
        task = asyncio.create_task(
            self._run(invocation), name=f"tool-{invocation.name}-{invocation.call_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, invocation: ToolInvocation) -> None:
        async with self._semaphore:
            registered = self._tools.get(invocation.name)
            started = time.monotonic()
            if registered is None:
                result: dict[str, Any] = {"ok": False, "error": f"unknown tool: {invocation.name}"}
            else:
                try:
                    result = await asyncio.wait_for(
                        registered.handler(invocation.arguments), registered.timeout_s
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    result = {"ok": False, "error": f"{registered.category}_timeout"}
                except Exception as exc:
                    result = {"ok": False, "error": str(exc) or type(exc).__name__}
                if not isinstance(result, dict):
                    result = {"ok": False, "error": "tool returned a non-dict result"}
            duration_ms = (time.monotonic() - started) * 1000.0
            output = json.dumps(result, ensure_ascii=False)
            if len(output) > MAX_RESULT_CHARS:
                result = {"ok": False, "error": "result_too_large"}
                output = json.dumps(result, ensure_ascii=False)
            if invocation.epoch != self._epoch_provider():
                self._record_event(
                    "tool.failed", name=invocation.name, call_id=invocation.call_id, error="stale_epoch"
                )
                return
            # Drop ourselves from the in-flight set before delivering, so busy()
            # inside the callback reflects only OTHER tools (the session disarms
            # its watchdog when the last in-flight tool finishes).
            current = asyncio.current_task()
            if current is not None:
                self._tasks.discard(current)
            try:
                await self._on_output(invocation, result, output, duration_ms)
            except Exception:
                logger.exception("tool output callback failed: %s", invocation.name)

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
```

Note: `deque` is imported for `RecentIds` (it uses one); keep whatever imports the pasted class needs and delete any that end up unused in `realtime.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tool_executor.py -v`
Expected: 4 PASS

- [ ] **Step 5: Run the full suite (the RecentIds move must not break the five importing test files)**

Run: `uv run ruff check . && uv run pytest`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add reachy_openai_realtime/tool_executor.py reachy_openai_realtime/realtime.py tests/test_tool_executor.py
git commit -m "feat: add ToolExecutor with registry and bounded async dispatch (#21)"
```

---

### Task 2: Guard rails — duplicates, stale epochs, oversized and malformed results

**Files:**
- Modify: `reachy_openai_realtime/tool_executor.py` (behavior exists from Task 1; this task pins it with tests and fixes anything the tests flush out)
- Test: `tests/test_tool_executor.py`

**Interfaces:**
- Consumes: everything Task 1 produced.
- Produces: the guard contract later tasks rely on — `submit` returns `False` on duplicate/stale and records `tool.failed` with `error="duplicate_call_id"` / `"stale_epoch"`; completion after an epoch bump delivers nothing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tool_executor.py`:

```python
def test_duplicate_call_id_is_dropped():
    async def scenario():
        executor, outputs, events, _ = make_executor()

        async def echo(arguments):
            return {"ok": True}

        executor.register("echo", echo, timeout_s=1.0)
        first = await executor.submit(ToolInvocation(1, "call_dup", "echo", {}))
        second = await executor.submit(ToolInvocation(1, "call_dup", "echo", {}))
        await drain(executor)
        assert first is True and second is False
        assert len(outputs.outputs) == 1
        assert ("tool.failed", {"name": "echo", "call_id": "call_dup", "error": "duplicate_call_id"}) in events.events

    asyncio.run(scenario())


def test_stale_epoch_rejected_at_submit():
    async def scenario():
        executor, outputs, events, epoch = make_executor()
        epoch["value"] = 2
        accepted = await executor.submit(ToolInvocation(1, "call_old", "echo", {}))
        assert accepted is False
        assert outputs.outputs == []
        assert events.events[-1][1]["error"] == "stale_epoch"

    asyncio.run(scenario())


def test_stale_epoch_dropped_at_completion():
    async def scenario():
        executor, outputs, events, epoch = make_executor()
        release = asyncio.Event()

        async def slow(arguments):
            await release.wait()
            return {"ok": True}

        executor.register("slow", slow, timeout_s=5.0)
        await executor.submit(ToolInvocation(1, "call_racy", "slow", {}))
        epoch["value"] = 2  # reconnect happened mid-flight
        release.set()
        await drain(executor)
        assert outputs.outputs == []
        assert events.events[-1][1]["error"] == "stale_epoch"

    asyncio.run(scenario())


def test_oversized_result_is_clamped():
    async def scenario():
        executor, outputs, events, _ = make_executor()

        async def bloated(arguments):
            return {"ok": True, "blob": "x" * 20_000}

        executor.register("bloated", bloated, timeout_s=1.0)
        await executor.submit(ToolInvocation(1, "call_big", "bloated", {}))
        await drain(executor)
        (_, result, output, _) = outputs.outputs[0]
        assert result == {"ok": False, "error": "result_too_large"}
        assert len(output) < 100

    asyncio.run(scenario())


def test_non_dict_result_becomes_structured_error():
    async def scenario():
        executor, outputs, events, _ = make_executor()

        async def wrong(arguments):
            return "not a dict"

        executor.register("wrong", wrong, timeout_s=1.0)
        await executor.submit(ToolInvocation(1, "call_wrong", "wrong", {}))
        await drain(executor)
        (_, result, _, _) = outputs.outputs[0]
        assert result["ok"] is False
        assert "non-dict" in result["error"]

    asyncio.run(scenario())
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_tool_executor.py -v`
Expected: the five new tests PASS against Task 1's implementation. If any fails, the implementation has a real bug — fix `tool_executor.py` until they pass (the tests are the contract; do not weaken them).

- [ ] **Step 3: Commit**

```bash
git add tests/test_tool_executor.py reachy_openai_realtime/tool_executor.py
git commit -m "test: pin ToolExecutor guard rails (duplicates, stale epochs, result limits)"
```

---

### Task 3: Timeouts, error mapping, bounded concurrency, cancellation

**Files:**
- Modify: `reachy_openai_realtime/tool_executor.py` (as needed)
- Test: `tests/test_tool_executor.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: timeout error strings `f"{category}_timeout"` (e.g. `"motion_timeout"`), `cancel_all()` semantics (no output for cancelled tools, `busy()` becomes False), concurrency cap of `max_parallel`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tool_executor.py`:

```python
def test_timeout_maps_to_category_error():
    async def scenario():
        executor, outputs, events, _ = make_executor()

        async def stuck(arguments):
            await asyncio.sleep(10)
            return {"ok": True}

        executor.register("nod", stuck, timeout_s=0.05, category="motion")
        await executor.submit(ToolInvocation(1, "call_t", "nod", {}))
        await drain(executor)
        (_, result, _, duration_ms) = outputs.outputs[0]
        assert result == {"ok": False, "error": "motion_timeout"}
        assert duration_ms >= 50.0

    asyncio.run(scenario())


def test_handler_exception_becomes_structured_result():
    async def scenario():
        executor, outputs, events, _ = make_executor()

        async def broken(arguments):
            raise ValueError("unknown emotion: zoomies")

        executor.register("play_emotion", broken, timeout_s=1.0)
        await executor.submit(ToolInvocation(1, "call_e", "play_emotion", {}))
        await drain(executor)
        (_, result, _, _) = outputs.outputs[0]
        assert result == {"ok": False, "error": "unknown emotion: zoomies"}

    asyncio.run(scenario())


def test_concurrency_is_bounded_at_two():
    async def scenario():
        state = {"active": 0, "peak": 0}

        async def slow(arguments):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.05)
            state["active"] -= 1
            return {"ok": True}

        executor, outputs, events, _ = make_executor()
        executor.register("slow", slow, timeout_s=5.0)
        for index in range(4):
            await executor.submit(ToolInvocation(1, f"call_{index}", "slow", {}))
        await drain(executor)
        assert state["peak"] <= 2
        assert len(outputs.outputs) == 4

    asyncio.run(scenario())


def test_cancel_all_stops_in_flight_tools_without_output():
    async def scenario():
        executor, outputs, events, _ = make_executor()

        async def forever(arguments):
            await asyncio.Event().wait()
            return {"ok": True}

        executor.register("forever", forever, timeout_s=60.0)
        await executor.submit(ToolInvocation(1, "call_c", "forever", {}))
        assert executor.busy() is True
        await executor.cancel_all()
        assert executor.busy() is False
        assert outputs.outputs == []

    asyncio.run(scenario())
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_tool_executor.py -v`
Expected: PASS against the Task 1 implementation; fix `tool_executor.py` if any contract test fails.

- [ ] **Step 3: Run the canonical check and commit**

Run: `uv run ruff check . && uv run pytest`

```bash
git add tests/test_tool_executor.py reachy_openai_realtime/tool_executor.py
git commit -m "test: pin ToolExecutor timeouts, concurrency bound, and cancellation"
```

---

### Task 4: Wire the executor into `realtime.py`

**Files:**
- Modify: `reachy_openai_realtime/realtime.py`
- Test: `tests/test_realtime_tool_dispatch.py` (create)

**Interfaces:**
- Consumes: the full `ToolExecutor` surface from Tasks 1–3.
- Produces (the memory plan builds on these exact names):
  - `RealtimeRobotSession.tools: ToolExecutor` built in `__init__`.
  - `RealtimeRobotSession._finish_tool_call(invocation, result, output, duration_ms)` — the executor's `on_output`; also called directly for argument-parse failures.
  - `RealtimeRobotSession._register_motion_tools()` — idempotent; called in `__init__` and at the top of `_run_connection`.
  - `response.done` handling that enters `TOOL_EXECUTION` when `self.tools.busy()`, so late tool outputs flush from `TOOL_EXECUTION` (or `LISTENING`).

- [ ] **Step 1: Make the production changes**

All edits in `reachy_openai_realtime/realtime.py`.

1. Extend the Task 1 import:

```python
from .tool_executor import (
    MOTION_TOOL_TIMEOUT_S,
    TOOL_WATCHDOG_GRACE_S,
    RecentIds,
    ToolExecutor,
    ToolInvocation,
)
```

2. In `__init__` (after `self.watchdog = DeadlineWatchdog()` — `self.status` and `self.motion` are already set by then), build the executor and register motion tools:

```python
        self.tools = ToolExecutor(
            epoch_provider=lambda: self.connection_epoch,
            on_output=self._finish_tool_call,
            record_event=self.status.record_event,
        )
        self._register_motion_tools()
```

3. Add the two helpers near `_handle_tool_call`:

```python
    def _register_motion_tools(self) -> None:
        # Idempotent: register() overwrites. Re-run at connection start so tools
        # from catalogs that finished loading after __init__ (play_emotion,
        # play_dance) are registered, matching _session_config's fresh
        # tool_definitions() call.
        for definition in self.motion.tool_definitions():
            name = str(definition.get("name", ""))
            if name:
                self.tools.register(
                    name,
                    self._motion_tool_handler(name),
                    timeout_s=MOTION_TOOL_TIMEOUT_S,
                    category="motion",
                )

    def _motion_tool_handler(self, name: str):
        async def handle(arguments: dict[str, Any]) -> dict[str, Any]:
            return await asyncio.to_thread(self.motion.submit, name, arguments)

        return handle
```

4. At the very top of `_run_connection` (before the `async with self.client.realtime.connect(...)` line), add `self._register_motion_tools()`.

5. Replace `_handle_tool_call` (currently lines ~1016–1036) entirely:

```python
    async def _handle_tool_call(self, event: Any) -> None:
        call_id = str(event.call_id)
        name = str(event.name)
        self.status.record_event("tool.requested", name=name, call_id=call_id)
        try:
            arguments = json.loads(event.arguments or "{}")
            if not isinstance(arguments, dict):
                raise TypeError("モーション引数がdictではありません")
        except (TypeError, json.JSONDecodeError) as exc:
            invocation = ToolInvocation(self.connection_epoch, call_id, name, {})
            result = {"ok": False, "error": str(exc)}
            await self._finish_tool_call(invocation, result, json.dumps(result, ensure_ascii=False), 0.0)
            return
        invocation = ToolInvocation(self.connection_epoch, call_id, name, arguments)
        # #21: the tool_response watchdog stays armed around executor dispatch.
        # Explicit deadline: the per-tool timeout (10-15s) exceeds the 5s
        # DEFAULT_DEADLINES entry, and a legal slow tool must not trip a reconnect.
        self.watchdog.arm("tool_response", self.tools.timeout_for(name) + TOOL_WATCHDOG_GRACE_S)
        accepted = await self.tools.submit(invocation)
        if not accepted and not self.tools.busy():
            self.watchdog.disarm("tool_response")
```

6. Add `_finish_tool_call` right after it (this is today's post-`submit` bookkeeping from the old `_handle_tool_call`, moved and extended with the late-flush path):

```python
    async def _finish_tool_call(
        self, invocation: ToolInvocation, result: dict[str, Any], output: str, duration_ms: float
    ) -> None:
        ok = bool(result.get("ok"))
        if ok:
            self.status.record_event("tool.completed", name=invocation.name, call_id=invocation.call_id)
        else:
            self.status.record_event(
                "tool.failed",
                name=invocation.name,
                call_id=invocation.call_id,
                error=str(result.get("error", "unknown"))[:120],
            )
        self.status.metrics.observe_ms("tool_duration_ms", duration_ms)
        if not ok:
            self.status.metrics.increment("tool_error_count")
        self.status.record_motion(invocation.name, invocation.arguments, ok)
        self._pending_tool_outputs.append((invocation.epoch, invocation.call_id, output))
        if not self.tools.busy():
            self.watchdog.disarm("tool_response")
        # Late-output path: execution is async now, so completion can arrive
        # AFTER response.done already ran. In that case nobody else will flush.
        if self._response_generation_done and self.fsm.state in (
            SessionState.TOOL_EXECUTION,
            SessionState.LISTENING,
        ):
            await self._flush_tool_outputs()
```

7. In the `response.done` handler, change the tool-output branch (currently lines ~970–973) from:

```python
                    has_tool_outputs = bool(self._pending_tool_outputs)
                    if has_tool_outputs:
                        self.fsm.transition(SessionState.TOOL_EXECUTION, reason="tool_outputs_pending")
                        await self._flush_tool_outputs()
```

to:

```python
                    has_tool_outputs = bool(self._pending_tool_outputs)
                    if has_tool_outputs or self.tools.busy():
                        self.fsm.transition(SessionState.TOOL_EXECUTION, reason="tool_outputs_pending")
                        if has_tool_outputs:
                            await self._flush_tool_outputs()
```

(the `else:` branch below is untouched). `TOOL_EXECUTION` is legal from both `WAITING_RESPONSE` and `ASSISTANT_SPEAKING`, which are the only states `response.done` fires in with tools in flight.

8. In `reset_connection_state`, next to `self._pending_tool_outputs.clear()`, add:

```python
        await self.tools.cancel_all()
```

(`reset_connection_state` is already `async`.)

- [ ] **Step 2: Write the integration tests**

Create `tests/test_realtime_tool_dispatch.py`. Bare-session pattern; look at `tests/test_chaos_protocol.py` first — it already fakes a connection for `_flush_tool_outputs` and shows which fields that method touches. If `_flush_tool_outputs` or `_current_language` needs a field not listed below, add it to the helper the same way that file does.

```python
# ABOUTME: Integration tests for ToolExecutor wiring in RealtimeRobotSession:
# ABOUTME: async dispatch, late-output flush, stale epochs, watchdog bracketing.
import asyncio
import json
from types import SimpleNamespace

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
from reachy_openai_realtime.tool_executor import RecentIds, ToolExecutor, ToolInvocation


class FakeMetrics:
    def __init__(self):
        self.observations = []
        self.counters = {}

    def observe_ms(self, name, value):
        self.observations.append((name, value))

    def increment(self, name):
        self.counters[name] = self.counters.get(name, 0) + 1


class FakeStatus:
    def __init__(self):
        self.events = []
        self.motions = []
        self.response_requests = 0
        self.metrics = FakeMetrics()

    def record_event(self, event, **fields):
        self.events.append((event, fields))

    def record_motion(self, name, arguments, ok):
        self.motions.append((name, arguments, ok))

    def record_response_request(self):
        self.response_requests += 1


class FakeConnection:
    def __init__(self):
        self.items = []
        self.responses = []
        self.conversation = SimpleNamespace(item=SimpleNamespace(create=self._create_item))
        self.response = SimpleNamespace(create=self._create_response)

    async def _create_item(self, item):
        self.items.append(item)

    async def _create_response(self, response):
        self.responses.append(response)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_session(clock=None):
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.connection = FakeConnection()
    session.connection_epoch = 1
    session.status = FakeStatus()
    session.watchdog = DeadlineWatchdog(clock=clock) if clock else DeadlineWatchdog()
    session.fsm = SessionStateMachine()
    session.config = AppConfig()
    session._language_provider = None
    session._pending_tool_outputs = []
    session._response_generation_done = False
    session._interrupted_response_ids = RecentIds()
    session.tools = ToolExecutor(
        epoch_provider=lambda: session.connection_epoch,
        on_output=session._finish_tool_call,
        record_event=session.status.record_event,
    )
    return session


async def wait_until(predicate, timeout_s=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        assert loop.time() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


def make_tool_event(call_id="call_1", name="echo", arguments=None):
    return SimpleNamespace(
        call_id=call_id, name=name, arguments=json.dumps(arguments if arguments is not None else {"x": 1})
    )


def test_tool_call_runs_async_and_output_lands_in_pending():
    async def scenario():
        session = make_session()

        async def echo(arguments):
            return {"ok": True, "echo": arguments}

        session.tools.register("echo", echo, timeout_s=1.0)
        await session._handle_tool_call(make_tool_event())
        await wait_until(lambda: session._pending_tool_outputs)
        epoch, call_id, output = session._pending_tool_outputs[0]
        assert (epoch, call_id) == (1, "call_1")
        assert json.loads(output)["ok"] is True
        assert session.status.motions == [("echo", {"x": 1}, True)]
        event_names = [event for event, _ in session.status.events]
        assert "tool.requested" in event_names and "tool.completed" in event_names

    asyncio.run(scenario())


def test_late_output_flushes_after_response_done():
    async def scenario():
        session = make_session()
        release = asyncio.Event()

        async def slow(arguments):
            await release.wait()
            return {"ok": True}

        session.tools.register("slow", slow, timeout_s=5.0)
        await session._handle_tool_call(make_tool_event(name="slow"))
        # response.done ran while the tool was still in flight:
        session._response_generation_done = True
        session.fsm._state = SessionState.TOOL_EXECUTION
        assert session.connection.items == []
        release.set()
        await wait_until(lambda: session.connection.responses)
        assert session.connection.items[0]["type"] == "function_call_output"
        assert session.fsm.state is SessionState.WAITING_RESPONSE

    asyncio.run(scenario())


def test_stale_epoch_output_is_never_flushed():
    async def scenario():
        session = make_session()
        release = asyncio.Event()

        async def slow(arguments):
            await release.wait()
            return {"ok": True}

        session.tools.register("slow", slow, timeout_s=5.0)
        await session._handle_tool_call(make_tool_event(name="slow"))
        session.connection_epoch = 2  # reconnect while tool in flight
        release.set()
        await wait_until(lambda: not session.tools.busy())
        await asyncio.sleep(0.02)
        assert session._pending_tool_outputs == []
        assert session.connection.items == []

    asyncio.run(scenario())


def test_parse_error_produces_immediate_failure_output():
    async def scenario():
        session = make_session()
        event = SimpleNamespace(call_id="call_bad", name="echo", arguments="not json")
        await session._handle_tool_call(event)
        assert len(session._pending_tool_outputs) == 1
        _, call_id, output = session._pending_tool_outputs[0]
        assert call_id == "call_bad"
        assert json.loads(output)["ok"] is False
        assert session.status.metrics.counters.get("tool_error_count") == 1
        assert session.tools.busy() is False

    asyncio.run(scenario())


def test_watchdog_brackets_dispatch_with_tool_scaled_deadline():
    async def scenario():
        clock = FakeClock()
        session = make_session(clock=clock)
        release = asyncio.Event()

        async def slow(arguments):
            await release.wait()
            return {"ok": True}

        session.tools.register("nod", slow, timeout_s=10.0, category="motion")
        await session._handle_tool_call(make_tool_event(name="nod"))
        clock.advance(11.0)  # past the 5s default AND the 10s tool timeout...
        assert session.watchdog.expired() is None  # ...but under 10 + 2 grace
        clock.advance(1.5)
        expired = session.watchdog.expired()
        assert expired is not None and expired[0] == "tool_response"
        # Completion disarms:
        session.watchdog.arm("tool_response", 12.0)  # re-arm cleanly for the disarm check
        release.set()
        await wait_until(lambda: session._pending_tool_outputs)
        assert session.watchdog.expired() is None

    asyncio.run(scenario())


def test_executor_timeout_fires_before_watchdog():
    async def scenario():
        session = make_session()

        async def stuck(arguments):
            await asyncio.sleep(10)
            return {"ok": True}

        session.tools.register("nod", stuck, timeout_s=0.05, category="motion")
        await session._handle_tool_call(make_tool_event(name="nod"))
        await wait_until(lambda: session._pending_tool_outputs)
        _, _, output = session._pending_tool_outputs[0]
        assert json.loads(output) == {"ok": False, "error": "motion_timeout"}
        assert session.watchdog.expired() is None  # disarmed by completion, never expired

    asyncio.run(scenario())


def test_reset_connection_state_cancels_in_flight_tools():
    async def scenario():
        session = make_session()

        async def forever(arguments):
            await asyncio.Event().wait()
            return {"ok": True}

        session.tools.register("forever", forever, timeout_s=60.0)
        await session._handle_tool_call(make_tool_event(name="forever"))
        assert session.tools.busy() is True
        await session.tools.cancel_all()
        assert session.tools.busy() is False
        assert session._pending_tool_outputs == []

    asyncio.run(scenario())
```

Notes for the implementer:
- `session.fsm._state = SessionState.TOOL_EXECUTION` pokes the private field because a bare session never ran the connect sequence; this matches how existing bare-session tests position state.
- `test_reset_connection_state_cancels_in_flight_tools` exercises `cancel_all` through the session object rather than calling `reset_connection_state()` itself, because a bare session lacks the dozen playback/camera fields that method clears. The full `reset_connection_state` path is covered by the existing `tests/test_realtime_reset.py` — extend its session setup with a `tools` executor if it constructs sessions that reach the new line.

- [ ] **Step 3: Run the new tests**

Run: `uv run pytest tests/test_realtime_tool_dispatch.py -v`
Expected: PASS

- [ ] **Step 4: Run the full suite**

Run: `uv run ruff check . && uv run pytest`
Expected: all green. Existing tests that call the OLD `_handle_tool_call` synchronous contract (grep for `_handle_tool_call` under `tests/`) may need their assertions updated from "result appended synchronously" to "await drain then assert" — update those tests to the new async contract; do not weaken what they verify.

- [ ] **Step 5: Commit**

```bash
git add reachy_openai_realtime/realtime.py tests/test_realtime_tool_dispatch.py
git commit -m "feat: dispatch model tool calls through ToolExecutor off the receive loop (#21)"
```

---

### Task 5: Regression sweep and leftovers

**Files:**
- Modify: whatever the sweep finds (expected: none or small test updates)

**Interfaces:** none new.

- [ ] **Step 1: Sweep for stragglers**

Run each and inspect:

```bash
grep -rn "motion.submit" reachy_openai_realtime/ --include="*.py"
```
Expected survivors: `motion/manager.py` (the definition) and `tool_executor`-routed call in `_motion_tool_handler`. Any OTHER direct call from the receive-loop path is a missed conversion — fix it.

```bash
grep -rn "_handle_tool_call\|_finish_tool_call\|ToolExecutor" reachy_openai_realtime/ tests/ --include="*.py"
```
Confirm the call graph matches this plan (receive loop → `_handle_tool_call` → executor → `_finish_tool_call`).

- [ ] **Step 2: Full canonical check**

Run: `uv run ruff check . && uv run pytest`
Expected: green, zero warnings added.

- [ ] **Step 3: Commit (only if the sweep changed anything)**

```bash
git add <the specific files the sweep touched>
git commit -m "refactor: finish ToolExecutor conversion stragglers"
```

---

## Estimates

~170 LOC executor module, ~90 LOC realtime.py delta, ~420 LOC tests.

## Out of scope

Memory tools (the companion plan `2026-08-20-optmem-memory.md` registers them on this executor), camera tooling (constant reserved only), any change to `DEFAULT_DEADLINES` values, FSM changes.
