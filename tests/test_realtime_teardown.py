# ABOUTME: Regression tests for connection teardown on the stop flag — a stopped
# ABOUTME: session must tear down even when watchdog/supervisor loops ignore stop.
import asyncio
import threading

import pytest
from conftest import FakeRealtimeClient, ScriptedConnection, drive_fsm

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
from reachy_openai_realtime.session.watchdog import DeadlineWatchdog


class _TeardownMotion:
    """Records the enable-flag flips _run_connection's finally performs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def set_listening_enabled(self, enabled: bool) -> None:
        self.calls.append(("listening", enabled))

    def set_speaking_enabled(self, enabled: bool) -> None:
        self.calls.append(("speaking", enabled))

    def set_idle_enabled(self, enabled: bool) -> None:
        self.calls.append(("idle", enabled))


async def _honor_stop(stop_event) -> None:
    """Cooperative loop that returns promptly once stop flips (record/playback/event)."""
    while not stop_event.is_set():
        await asyncio.sleep(0.02)


async def _ignore_stop() -> None:
    """A loop that never watches the stop flag (watchdog). Only cancellation ends
    it — the exact shape that makes a bare gather() hang."""
    await asyncio.Event().wait()


async def _ignore_stop_arg(stop_event) -> None:
    """Supervisor variant: takes stop_event (real signature) but ignores it, still
    proving _await_tasks_or_stop tears down when a loop declines to watch stop."""
    await asyncio.Event().wait()


def _make_idle_session(
    *,
    record_loop=_honor_stop,
    playback_loop=_honor_stop,
    event_loop=_honor_stop,
    watchdog_loop=_ignore_stop,
    supervisor_loop=_ignore_stop_arg,
) -> RealtimeRobotSession:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.client = FakeRealtimeClient([ScriptedConnection([])])
    session.config = AppConfig()
    session.status = RuntimeStatus()
    session.fsm = SessionStateMachine()
    drive_fsm(session.fsm, SessionState.CONNECTING)  # so socket_open -> INITIALIZING is legal
    session.watchdog = DeadlineWatchdog()
    session.motion = _TeardownMotion()
    session.robot = type("Robot", (), {"media": object()})()  # no get_DoA -> no poller
    session.memory = None
    session.nap = None
    session._doa_poller = None
    session.connection_epoch = 1
    session._connected_epoch = 1
    session._memory_tools_active = False
    session._connected_at = None
    session._last_camera_item_id = None
    session._pending_camera_items = {}
    session._camera_add_events = {}
    session._camera_delete_events = {}
    # Isolate the gather/stop race from motion-tool and session-config wiring.
    session._register_motion_tools = lambda: None
    session._session_config = dict
    session._record_loop = record_loop
    session._playback_loop = playback_loop
    session._event_loop = event_loop
    session._watchdog_loop = watchdog_loop
    session._supervisor_loop = supervisor_loop
    return session


def test_stop_flag_tears_down_connection_when_loops_ignore_it() -> None:
    # Reproduces the sleep-teardown hang: record/playback/event return on stop,
    # but watchdog/supervisor never watch it. A bare asyncio.gather(*tasks) then
    # blocks until the socket drops, so setting the stop flag alone must still
    # tear the connection down promptly.
    session = _make_idle_session()
    stop_event = threading.Event()

    async def drive() -> None:
        run_conn = asyncio.create_task(session._run_connection(stop_event))
        await asyncio.sleep(0.15)  # let the socket open and the tasks start
        if run_conn.done():
            run_conn.result()  # surface a setup error instead of masking it
            raise AssertionError("connection ended before stop was set")
        stop_event.set()
        await asyncio.wait_for(run_conn, timeout=2.0)  # hangs today

    asyncio.run(drive())

    # The finally-block teardown ran: motion disabled, connection dropped.
    assert session.connection is None
    assert ("idle", False) in session.motion.calls


def test_task_exception_propagates_for_reconnect_when_not_stopped() -> None:
    # A connection task failing (socket drop, protocol error) with no stop set
    # must still surface as an exception from _run_connection so run()'s
    # classifier drives a reconnect. The stop-race must not swallow the error.
    boom = RuntimeError("socket dropped")

    async def raising_event(stop_event) -> None:
        await asyncio.sleep(0.02)
        raise boom

    session = _make_idle_session(event_loop=raising_event)
    stop_event = threading.Event()  # never set

    with pytest.raises(RuntimeError, match="socket dropped"):
        asyncio.run(session._run_connection(stop_event))

    # Even on the error path, the finally tore the connection down.
    assert session.connection is None
