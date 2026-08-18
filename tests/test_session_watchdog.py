# ABOUTME: Tests for the protocol deadline watchdog (session/watchdog.py).
# ABOUTME: Uses a FakeClock so no real wall-time is consumed in any test.
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
