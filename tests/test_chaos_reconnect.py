# ABOUTME: Chaos/integration tests for the run() reconnect loop. Exercises the real
# ABOUTME: wiring with scripted Realtime connections; verifies no thread or state leaks.
import asyncio
import os
import threading

from conftest import FakeRealtimeClient, FakeRecorder, ScriptedConnection, realtime_event
from test_realtime_manual_turn import BargeInMotion, FakeMedia, stereo_frame

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.recovery import SessionOutcome


class ChaosMedia(FakeMedia):
    """FakeMedia plus output-rate stub and the pipeline-restart surface the mic ladder may touch."""

    def __init__(self, frames) -> None:
        super().__init__(frames)
        self.recording_restarts = 0

    def get_output_audio_samplerate(self) -> int:
        return 24_000

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


def test_realtime_connected_and_disconnected_events_recorded() -> None:
    """realtime.connected fires after session.updated; realtime.disconnected fires on teardown."""
    stop_event = threading.Event()
    session_updated = realtime_event("session.updated", session=None)
    conn = ScriptedConnection(
        [session_updated], on_drained=stop_event.set, raise_after=ConnectionError("closed")
    )
    session = build_session([conn])
    session._sleep_unless_stopped = _instant_sleep

    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)

    asyncio.run(session.run(stop_event))

    recorded_names = [e for e, _ in recorder.events]
    assert "realtime.connected" in recorded_names, f"realtime.connected not recorded; got {recorded_names}"
    assert "realtime.disconnected" in recorded_names, f"realtime.disconnected not recorded; got {recorded_names}"

    connected_fields = next(f for e, f in recorder.events if e == "realtime.connected")
    disconnected_fields = next(f for e, f in recorder.events if e == "realtime.disconnected")
    assert "epoch" in connected_fields, "realtime.connected missing epoch"
    assert "epoch" in disconnected_fields, "realtime.disconnected missing epoch"
    assert connected_fields["epoch"] == disconnected_fields["epoch"]
