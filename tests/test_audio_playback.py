# ABOUTME: Tests for PlaybackBuffer (jitter buffer) and SpeakerWorker (speaker thread).
# ABOUTME: Covers FIFO ordering, drop-oldest overflow, epoch filtering, and worker lifecycle.
import time

import numpy as np

from reachy_openai_realtime.audio.playback import PlaybackBuffer, PlaybackChunk, SpeakerWorker


def chunk(ms: float, *, epoch: int = 1, response_id: str = "resp_1") -> PlaybackChunk:
    samples = int(24_000 * ms / 1000.0)
    return PlaybackChunk(
        epoch=epoch,
        response_id=response_id,
        pcm=np.zeros(samples, dtype=np.int16),
        duration_ms=ms,
        received_at=time.monotonic(),
    )


def test_push_pop_fifo_and_queued_ms_accounting() -> None:
    buffer = PlaybackBuffer()
    first = chunk(80.0)
    buffer.push(first)
    buffer.push(chunk(80.0))
    assert buffer.queued_ms() == 160.0
    assert buffer.pop_wait(0.1, current_epoch=1) is first
    assert buffer.queued_ms() == 80.0


def test_over_max_drops_oldest_until_under_limit() -> None:
    buffer = PlaybackBuffer(max_ms=500.0, hard_max_ms=10_000.0)
    results = [buffer.push(chunk(100.0)) for _ in range(7)]
    assert buffer.queued_ms() <= 500.0
    assert sum(result.dropped_ms for result in results) >= 200.0
    assert not any(result.overrun for result in results)


def test_hard_max_signals_overrun() -> None:
    buffer = PlaybackBuffer(max_ms=5_000.0, hard_max_ms=1_000.0)
    results = [buffer.push(chunk(200.0)) for _ in range(6)]
    assert results[-1].overrun is True


def test_pop_wait_skips_stale_epochs() -> None:
    buffer = PlaybackBuffer()
    buffer.push(chunk(100.0, epoch=1))
    buffer.push(chunk(100.0, epoch=2))
    popped = buffer.pop_wait(0.1, current_epoch=2)
    assert popped is not None
    assert popped.epoch == 2
    assert buffer.pop_wait(0.05, current_epoch=2) is None


def test_pop_wait_times_out_when_empty() -> None:
    buffer = PlaybackBuffer()
    started = time.monotonic()
    assert buffer.pop_wait(0.1, current_epoch=1) is None
    assert time.monotonic() - started < 1.0


def test_clear_returns_dropped_ms() -> None:
    buffer = PlaybackBuffer()
    buffer.push(chunk(150.0))
    buffer.push(chunk(150.0))
    assert buffer.clear() == 300.0
    assert buffer.queued_ms() == 0.0


class FakeSpeakerMedia:
    def __init__(self) -> None:
        self.pushed: list[np.ndarray] = []

    def push_audio_sample(self, data: np.ndarray) -> None:
        self.pushed.append(data)


def test_speaker_worker_writes_in_order_and_reports_writes() -> None:
    media = FakeSpeakerMedia()
    writes: list[float] = []
    worker = SpeakerWorker(media, on_write=lambda duration_ms, received_at: writes.append(duration_ms))
    worker.start()
    try:
        pcm = np.zeros((480, 2), dtype=np.float32)
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=1.0) is True
        deadline = time.monotonic() + 2.0
        while not media.pushed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(media.pushed) == 1
        assert writes == [20.0]
        assert worker.frames_total == 1
        assert worker.stalled(threshold_seconds=5.0) is False
    finally:
        worker.close()


def test_speaker_worker_submit_false_when_inbox_wedged() -> None:
    class WedgedMedia:
        def push_audio_sample(self, data: np.ndarray) -> None:
            time.sleep(10.0)

    worker = SpeakerWorker(WedgedMedia(), inbox_max=1)
    worker.start()
    try:
        pcm = np.zeros((480, 2), dtype=np.float32)
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.2) is True
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.2) is True  # queued
        assert worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.2) is False  # wedged
    finally:
        worker.close()


def test_speaker_worker_flush_drops_queued_audio() -> None:
    media = FakeSpeakerMedia()
    worker = SpeakerWorker(media, inbox_max=4)
    pcm = np.zeros((480, 2), dtype=np.float32)
    worker.submit(pcm, 20.0, time.monotonic(), timeout_seconds=0.1)  # worker not started: stays queued
    worker.flush()
    worker.start()
    try:
        time.sleep(0.1)
        assert media.pushed == []
    finally:
        worker.close()


def test_playback_overrun_cancels_response_and_returns_to_listening() -> None:
    import asyncio

    from conftest import drive_fsm

    from reachy_openai_realtime.realtime import RealtimeRobotSession, RecentIds
    from reachy_openai_realtime.runtime_status import RuntimeStatus
    from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine
    from reachy_openai_realtime.session.watchdog import DeadlineWatchdog
    from test_realtime_manual_turn import BargeInMedia, BargeInMotion, FakeConnection, FakeStopEvent

    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": BargeInMedia()})()
    session.motion = BargeInMotion()
    session.status = RuntimeStatus()
    session.connection = FakeConnection(FakeStopEvent())
    session.fsm = SessionStateMachine()
    drive_fsm(session.fsm, SessionState.ASSISTANT_SPEAKING)
    session.watchdog = DeadlineWatchdog()
    session._playback = PlaybackBuffer()
    session._speaker = SpeakerWorker(FakeSpeakerMedia())
    session._playback_io_lock = asyncio.Lock()
    session._current_response_id = "resp_overrun"
    session._interrupted_response_ids = RecentIds()

    asyncio.run(session._handle_playback_overrun(900.0))

    assert session.connection.response.cancelled == ["resp_overrun"]
    assert "resp_overrun" in session._interrupted_response_ids
    assert session.fsm.state is SessionState.LISTENING
    assert session.robot.media.audio.cleared == 1
    assert session.robot.media.recording_restarts == 1
