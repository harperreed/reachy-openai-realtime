# ABOUTME: Integration tests for the run() reconnect loop — fatal errors stop;
# ABOUTME: transient errors retry with incrementing epochs until stop is set.
import asyncio
import os
import threading

from conftest import FakeRecorder

from reachy_openai_realtime import realtime as realtime_mod
from reachy_openai_realtime.audio.capture import CaptureWorker
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.presence.manager import _EitherStop
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.recovery import SessionOutcome


class FatalConnectError(Exception):
    def __init__(self) -> None:
        super().__init__("invalid key")
        self.status_code = 401


def make_session(connect_error: Exception, attempts: list[int]) -> RealtimeRobotSession:
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-0000000000")

    class _FakeMotion:
        def stop_current(self) -> None:
            pass

        def set_idle_enabled(self, enabled: bool) -> None:
            pass

        def set_listening_enabled(self, enabled: bool) -> None:
            pass

        def set_speaking_enabled(self, enabled: bool) -> None:
            pass

        def tool_definitions(self) -> list:
            return []

    class _FakeMedia:
        def get_input_audio_samplerate(self) -> int:
            return 16_000

        def get_audio_sample(self):
            return None

    robot = type("Robot", (), {"media": _FakeMedia()})()
    capture = CaptureWorker(robot.media)
    capture.start()
    session = RealtimeRobotSession(
        robot=robot,
        motion=_FakeMotion(),
        config=AppConfig(),
        status=RuntimeStatus(),
        capture=capture,
    )

    async def failing_run_connection(stop_event: object) -> None:
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
    session._capture.close()


def test_transient_error_retries_with_new_epoch_until_stop() -> None:
    attempts: list[int] = []
    stop_event = threading.Event()

    class TransientError(ConnectionError):
        pass

    session = make_session(TransientError("wifi blip"), attempts)
    original_sleep = session._sleep_unless_stopped

    async def fast_sleep(event: object, seconds: float) -> None:
        if len(attempts) >= 3:
            stop_event.set()
        await original_sleep(event, 0.0)

    session._sleep_unless_stopped = fast_sleep  # type: ignore[method-assign]
    outcome = asyncio.run(session.run(stop_event))

    assert outcome is SessionOutcome.STOPPED
    assert attempts == [1, 2, 3]
    session._capture.close()


def test_noise_bail_stop_returns_distinct_outcome() -> None:
    attempts: list[int] = []
    session = make_session(ConnectionError("unused"), attempts)
    session._noise_bailed = True
    stop_event = threading.Event()
    stop_event.set()

    assert asyncio.run(session.run(stop_event)) is SessionOutcome.NOISE_BAIL

    session._capture.close()


def test_noise_bail_wins_when_fatal_error_arrives_after_stop_check() -> None:
    attempts: list[int] = []
    session = make_session(FatalConnectError(), attempts)
    app_stop = threading.Event()
    session_stop = threading.Event()
    combined_stop = _EitherStop(app_stop, session_stop)
    record_error = session.status.record_error

    def mark_noise_bail_after_stop_check(error: object) -> None:
        session._noise_bailed = True
        combined_stop.set()
        record_error(error)

    session.status.record_error = mark_noise_bail_after_stop_check  # type: ignore[method-assign]

    outcome = asyncio.run(session.run(combined_stop))

    assert outcome is SessionOutcome.NOISE_BAIL
    assert session_stop.is_set()
    assert not app_stop.is_set()
    assert attempts == [1]
    session._capture.close()


def test_deadline_wins_when_fatal_reconnect_finishes_in_same_wait_cycle(monkeypatch) -> None:
    monkeypatch.setattr(realtime_mod, "SESSION_LIMIT_SECONDS", 0)
    attempts: list[int] = []
    session = make_session(ConnectionError("unused"), attempts)
    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)
    app_stop = threading.Event()
    session_stop = threading.Event()
    combined_stop = _EitherStop(app_stop, session_stop)

    async def fatal_reconnect(stop_event: object) -> SessionOutcome:
        return SessionOutcome.FATAL_CONFIG

    async def finish_same_wait_cycle(tasks, *, return_when):
        task_set = set(tasks)
        stop_waiters = {
            task for task in task_set if task.get_coro().__qualname__.endswith("._await_stop")
        }
        assert return_when is asyncio.FIRST_COMPLETED
        assert len(stop_waiters) == 1
        completed = task_set - stop_waiters
        await asyncio.gather(*completed)
        return completed, stop_waiters

    session._run_reconnect_loop = fatal_reconnect  # type: ignore[method-assign]
    monkeypatch.setattr(realtime_mod.asyncio, "wait", finish_same_wait_cycle)
    try:
        outcome = asyncio.run(session.run(combined_stop))
    finally:
        session._capture.close()

    assert outcome is SessionOutcome.NOISE_BAIL
    assert session_stop.is_set()
    assert not app_stop.is_set()
    assert [event for event, _ in recorder.events].count("noise_bail.wall_clock") == 1


def test_external_stop_wins_when_deadline_finishes_in_same_wait_cycle(monkeypatch) -> None:
    monkeypatch.setattr(realtime_mod, "SESSION_LIMIT_SECONDS", 0)
    attempts: list[int] = []
    session = make_session(ConnectionError("unused"), attempts)
    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)
    stop_event = threading.Event()
    stop_event.set()

    async def stopped_reconnect(stop_event: object) -> SessionOutcome:
        return SessionOutcome.STOPPED

    async def finish_same_wait_cycle(tasks, *, return_when):
        assert return_when is asyncio.FIRST_COMPLETED
        task_set = set(tasks)
        await asyncio.gather(*task_set)
        return task_set, set()

    session._run_reconnect_loop = stopped_reconnect  # type: ignore[method-assign]
    monkeypatch.setattr(realtime_mod.asyncio, "wait", finish_same_wait_cycle)
    try:
        outcome = asyncio.run(session.run(stop_event))
    finally:
        session._capture.close()

    assert outcome is SessionOutcome.STOPPED
    assert [event for event, _ in recorder.events].count("noise_bail.wall_clock") == 0


def test_run_wall_clock_ceiling_cancels_blocked_connection_attempt(monkeypatch) -> None:
    monkeypatch.setattr(realtime_mod, "SESSION_LIMIT_SECONDS", 0.05)
    attempts: list[int] = []
    cancelled: list[bool] = []
    session = make_session(ConnectionError("unused"), attempts)
    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)
    app_stop = threading.Event()
    session_stop = threading.Event()
    combined_stop = _EitherStop(app_stop, session_stop)

    async def blocked_connection(stop_event: object) -> None:
        attempts.append(session.connection_epoch)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    session._run_connection = blocked_connection  # type: ignore[method-assign]
    try:
        outcome = asyncio.run(asyncio.wait_for(session.run(combined_stop), timeout=0.5))
    finally:
        session._capture.close()

    wall_clock_events = [fields for event, fields in recorder.events if event == "noise_bail.wall_clock"]
    assert outcome is SessionOutcome.NOISE_BAIL
    assert attempts == [1]
    assert cancelled == [True]
    assert session_stop.is_set()
    assert not app_stop.is_set()
    assert session.fsm.state.name == "DISCONNECTED"
    assert len(wall_clock_events) == 1
    assert wall_clock_events[0]["session_age_seconds"] >= 0.05
    assert wall_clock_events[0]["limit_seconds"] == 0.05
