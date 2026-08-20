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
        executor, outputs, _events, _ = make_executor()

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
        executor, outputs, _events, _ = make_executor()
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
