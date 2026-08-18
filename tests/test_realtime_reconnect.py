# ABOUTME: Integration tests for the run() reconnect loop — fatal errors stop;
# ABOUTME: transient errors retry with incrementing epochs until stop is set.
import asyncio
import os
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

    session = RealtimeRobotSession(
        robot=None,
        motion=_FakeMotion(),
        config=AppConfig(),
        status=RuntimeStatus(),
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
