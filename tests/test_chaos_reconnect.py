# ABOUTME: Chaos/integration tests for the run() reconnect loop. Exercises the real
# ABOUTME: wiring with scripted Realtime connections; verifies no thread or state leaks.
import asyncio
import os
import threading

import pytest
from conftest import FakeRealtimeClient, FakeRecorder, ScriptedConnection, realtime_event
from test_realtime_manual_turn import BargeInMotion, FakeMedia, stereo_frame

from reachy_openai_realtime.audio.capture import AudioPipelineStalled, AudioRecoveryLadder
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


# ---------------------------------------------------------------------------
# 9a: Watchdog fires → full reconnect cycle
# ---------------------------------------------------------------------------


def test_watchdog_fires_reconnect_and_records_events(monkeypatch) -> None:
    """Shrink session_update deadline so the watchdog fires before session.updated
    arrives; verify the session rebuilds with an incremented epoch and that
    watchdog.triggered + realtime.reconnect are both recorded."""
    import reachy_openai_realtime.session.watchdog as _wd_mod

    # Monkeypatch the deadline dict so arm("session_update") expires in 0.001 s.
    # The watchdog poll interval is 0.25 s by default; the deadline is already
    # expired on the first check after asyncio.sleep(0.25).
    monkeypatch.setitem(_wd_mod.DEFAULT_DEADLINES, "session_update", 0.001)

    stop_event = threading.Event()

    # First connection: idles without ever sending session.updated → watchdog fires.
    # ScriptedConnection with no events parks in `async for` forever until the
    # watchdog task cancels all sibling tasks by raising WatchdogTimeout.
    first = ScriptedConnection([])

    # Second connection: sends session.updated, then on_drained sets stop so run()
    # exits STOPPED instead of attempting a third connection.
    session_updated = realtime_event("session.updated", session=None)
    second = ScriptedConnection(
        [session_updated], on_drained=stop_event.set, raise_after=ConnectionError("server closed")
    )

    session = build_session([first, second])
    session._sleep_unless_stopped = _instant_sleep  # collapse reconnect backoff

    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)

    outcome = asyncio.run(session.run(stop_event))

    assert outcome is SessionOutcome.STOPPED
    # Epoch 2 means a reconnect happened (epoch 1 = first attempt, epoch 2 = second).
    assert session.connection_epoch == 2, f"expected epoch 2, got {session.connection_epoch}"

    recorded_names = [e for e, _ in recorder.events]
    assert "watchdog.triggered" in recorded_names, (
        f"watchdog.triggered not recorded; got {recorded_names}"
    )
    assert "realtime.reconnect" in recorded_names, (
        f"realtime.reconnect not recorded; got {recorded_names}"
    )

    watchdog_fields = next(f for e, f in recorder.events if e == "watchdog.triggered")
    assert watchdog_fields["operation"] == "session_update"


# ---------------------------------------------------------------------------
# 9b: AudioPipelineStalled escalates out of run()
# ---------------------------------------------------------------------------


class EmptyChaosMedia(ChaosMedia):
    """ChaosMedia that yields exactly zero audio frames — triggers the mic stall ladder."""

    def __init__(self) -> None:
        super().__init__([])  # empty frames → get_audio_sample() immediately returns None

    def get_audio_sample(self) -> None:
        return None


def test_audio_pipeline_stalled_escalates_and_cleans_up(monkeypatch) -> None:
    """AudioRecoveryLadder walks restart_capture → restart_media → restart_session fast
    (ladder thresholds injected at ~0 ms); session.run() raises AudioPipelineStalled;
    capture/speaker threads are joined afterwards."""
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-chaos-key-0000000000")

    # Build a session with media that never yields audio frames.
    robot = type("Robot", (), {"media": EmptyChaosMedia()})()
    session = RealtimeRobotSession(robot, BargeInMotion(), AppConfig(), RuntimeStatus())

    # Inject a fast-escalating ladder (stall after 0.001 s, cooldown 0.001 s).
    session._mic_ladder = AudioRecoveryLadder(stall_seconds=0.001, cooldown_seconds=0.001)

    # The connection sends session.updated so the event loop starts; then idles.
    # The record loop will immediately see frame_age >> stall_seconds.
    session_updated = realtime_event("session.updated", session=None)
    conn = ScriptedConnection([session_updated])
    session.client = FakeRealtimeClient([conn])

    recorder = FakeRecorder()
    session.status.attach_recorder(recorder)

    stop_event = threading.Event()

    with pytest.raises(AudioPipelineStalled):
        asyncio.run(session.run(stop_event))

    # Threads must be cleaned up.
    # CaptureWorker.close() sets _thread = None after joining; SpeakerWorker.close()
    # does the same.  Assert via the session's own worker references, not by name —
    # other tests in the suite may leave daemon threads with the same name and would
    # cause false negatives.
    assert session._capture is None, "CaptureWorker reference not cleared after stall"
    assert session._speaker._thread is None, (
        "SpeakerWorker._thread not cleared after stall — close() did not join"
    )

    # Flight recorder must contain at least one audio.capture.stalled event.
    recorded_names = [e for e, _ in recorder.events]
    assert "audio.capture.stalled" in recorded_names, (
        f"audio.capture.stalled not recorded; got {recorded_names}"
    )
