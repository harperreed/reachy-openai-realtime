# ABOUTME: Integration tests for ToolExecutor wiring in RealtimeRobotSession:
# ABOUTME: async dispatch, late-output flush, stale epochs, watchdog bracketing.
import asyncio
import json
from types import SimpleNamespace

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
from reachy_openai_realtime.tool_executor import RecentIds, ToolExecutor


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
