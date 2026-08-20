# ABOUTME: Tests for the supervisor policy module (spec §24): RestartBudget and
# ABOUTME: FSM-inactivity coroutine. Pure policy tests plus session integration.
from __future__ import annotations

import asyncio
import time
from typing import Any

from conftest import FakeRecorder, ScriptedConnection, realtime_event

# ---------------------------------------------------------------------------
# Step 1: Pure policy tests for RestartBudget
# ---------------------------------------------------------------------------


def test_restart_budget_stays_calm_under_limit() -> None:
    from reachy_openai_realtime.session.supervisor import RestartBudget

    budget = RestartBudget(limit=5, window_seconds=600.0)
    assert [budget.record_restart(now) for now in (0.0, 100.0, 200.0, 300.0)] == [False] * 4


def test_restart_budget_escalates_at_limit_within_window() -> None:
    from reachy_openai_realtime.session.supervisor import RestartBudget

    budget = RestartBudget(limit=5, window_seconds=600.0)
    for now in (0.0, 100.0, 200.0, 300.0):
        budget.record_restart(now)
    assert budget.record_restart(400.0) is True


def test_restart_budget_forgets_outside_window() -> None:
    from reachy_openai_realtime.session.supervisor import RestartBudget

    budget = RestartBudget(limit=5, window_seconds=600.0)
    for now in (0.0, 1.0, 2.0, 3.0):
        budget.record_restart(now)
    assert budget.record_restart(700.0) is False  # first four aged out


# ---------------------------------------------------------------------------
# Step 5: FSM-inactivity supervisor integration test
# ---------------------------------------------------------------------------


def _make_fake_robot():
    """Minimal robot stub for RealtimeRobotSession."""
    from reachy_mini.utils import create_head_pose

    class FakeMedia:
        def __init__(self) -> None:
            self.camera = None

        def get_input_audio_samplerate(self) -> int:
            return 16_000

        def get_output_audio_samplerate(self) -> int:
            return 24_000

        def start_recording(self) -> None:
            pass

        def stop_recording(self) -> None:
            pass

        def start_playing(self) -> None:
            pass

        def stop_playing(self) -> None:
            pass

        def pop(self, timeout: float) -> Any:
            # Block briefly so the record loop doesn't spin; return None = no data.
            time.sleep(min(timeout, 0.01))
            return None

    class FakeRobot:
        def __init__(self) -> None:
            self.media = FakeMedia()

        def get_current_head_pose(self) -> Any:
            return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

        def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
            return ([0.0] * 7, [0.0, 0.0])

        def set_target(self, head: Any = None, antennas: Any = None, body_yaw: float | None = None) -> None:
            pass

        def goto_target(
            self, head: Any = None, antennas: Any = None, duration: float = 0.5, body_yaw: float | None = 0.0
        ) -> None:
            pass

        def cancel_move(self) -> None:
            pass

    return FakeRobot()


def test_supervisor_fires_on_fsm_inactivity(monkeypatch) -> None:
    """Supervisor raises WatchdogTimeout when FSM is stuck in WAITING_RESPONSE.

    Uses tiny timing constants so the test finishes in < 1 s.
    """
    import reachy_openai_realtime.realtime as realtime_mod
    import reachy_openai_realtime.session.supervisor as supervisor_mod

    monkeypatch.setattr(supervisor_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    monkeypatch.setattr(supervisor_mod, "FSM_INACTIVITY_LIMIT_SECONDS", 0.05)
    # Also patch the names as they appear in realtime.py's namespace
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    monkeypatch.setattr(realtime_mod, "FSM_INACTIVITY_LIMIT_SECONDS", 0.05)

    from reachy_openai_realtime.config import AppConfig
    from reachy_openai_realtime.motion import MotionManager
    from reachy_openai_realtime.realtime import RealtimeRobotSession
    from reachy_openai_realtime.runtime_status import RuntimeStatus
    from reachy_openai_realtime.usage import UsageTracker

    fake_recorder = FakeRecorder()
    status = RuntimeStatus(UsageTracker(None))
    status.attach_recorder(fake_recorder)

    robot = _make_fake_robot()
    motion = MotionManager(robot)

    config = AppConfig.from_env()

    # Raise all ordinary watchdog deadlines so only the supervisor fires.
    from reachy_openai_realtime.session.watchdog import DEFAULT_DEADLINES

    patched_deadlines = {k: 300.0 for k in DEFAULT_DEADLINES}
    monkeypatch.setattr("reachy_openai_realtime.session.watchdog.DEFAULT_DEADLINES", patched_deadlines)

    # session.updated → FSM goes LISTENING then WAITING_RESPONSE (via greeting).
    # We don't send response.created so the FSM stays at WAITING_RESPONSE until
    # the supervisor fires.
    events = [
        realtime_event("session.updated"),
    ]

    connection = ScriptedConnection(events)

    class _ctx:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        async def __aenter__(self) -> Any:
            return self._conn

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr(
        "reachy_openai_realtime.realtime.AsyncOpenAI",
        lambda: type("FakeClient", (), {"realtime": type("RT", (), {"connect": lambda self, **kw: _ctx(connection)})()})(),
    )

    stop_event = asyncio.Event()

    session = RealtimeRobotSession(robot, motion, config, status)
    motion.start()

    async def _run_bounded() -> Any:
        try:
            return await asyncio.wait_for(session.run(stop_event), timeout=5.0)
        except asyncio.TimeoutError:
            stop_event.set()
            return None

    asyncio.run(_run_bounded())
    motion.close()

    # Supervisor should have fired: supervisor.intervention event recorded.
    event_names = [e for e, _ in fake_recorder.events]
    assert "supervisor.intervention" in event_names, (
        f"Expected supervisor.intervention in events; got {event_names}"
    )

    # The FSM-inactivity teardown must produce a genuine reconnect — epoch 2 means
    # the supervisor's WatchdogTimeout caused the run loop to start a fresh attempt.
    assert session.connection_epoch == 2, (
        f"Expected epoch 2 after supervisor-triggered reconnect, got {session.connection_epoch}"
    )


def test_supervisor_silent_when_listening(monkeypatch) -> None:
    """Supervisor does NOT fire when FSM stays in LISTENING (the happy idle path)."""
    import reachy_openai_realtime.realtime as realtime_mod
    import reachy_openai_realtime.session.supervisor as supervisor_mod

    # Poll fast but inactivity limit is long — supervisor must stay quiet for a
    # session that idles in LISTENING.
    monkeypatch.setattr(supervisor_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    monkeypatch.setattr(supervisor_mod, "FSM_INACTIVITY_LIMIT_SECONDS", 999.0)
    monkeypatch.setattr(realtime_mod, "SUPERVISOR_POLL_SECONDS", 0.01)
    monkeypatch.setattr(realtime_mod, "FSM_INACTIVITY_LIMIT_SECONDS", 999.0)

    from reachy_openai_realtime.config import AppConfig
    from reachy_openai_realtime.motion import MotionManager
    from reachy_openai_realtime.realtime import RealtimeRobotSession
    from reachy_openai_realtime.runtime_status import RuntimeStatus
    from reachy_openai_realtime.session.watchdog import DEFAULT_DEADLINES
    from reachy_openai_realtime.usage import UsageTracker

    patched_deadlines = {k: 300.0 for k in DEFAULT_DEADLINES}
    monkeypatch.setattr("reachy_openai_realtime.session.watchdog.DEFAULT_DEADLINES", patched_deadlines)

    fake_recorder = FakeRecorder()
    status = RuntimeStatus(UsageTracker(None))
    status.attach_recorder(fake_recorder)

    robot = _make_fake_robot()
    motion = MotionManager(robot)
    config = AppConfig.from_env()

    events = [realtime_event("session.updated")]
    connection = ScriptedConnection(events)

    class _ctx:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        async def __aenter__(self) -> Any:
            return self._conn

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr(
        "reachy_openai_realtime.realtime.AsyncOpenAI",
        lambda: type("FakeClient", (), {"realtime": type("RT", (), {"connect": lambda self, **kw: _ctx(connection)})()})(),
    )

    stop_event = asyncio.Event()

    session = RealtimeRobotSession(robot, motion, config, status)
    motion.start()

    # Let the session idle in LISTENING for ~150ms, then stop it cleanly.
    async def _run_bounded() -> Any:
        run_task = asyncio.create_task(session.run(stop_event))
        await asyncio.sleep(0.15)
        stop_event.set()
        try:
            return await asyncio.wait_for(run_task, timeout=3.0)
        except asyncio.TimeoutError:
            return None

    asyncio.run(_run_bounded())
    motion.close()

    event_names = [e for e, _ in fake_recorder.events]
    assert "supervisor.intervention" not in event_names, (
        f"supervisor.intervention fired unexpectedly in LISTENING; events={event_names}"
    )
