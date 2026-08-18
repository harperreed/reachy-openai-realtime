# ABOUTME: Tests for PlaybackBuffer (jitter buffer) and SpeakerWorker (speaker thread).
# ABOUTME: Covers FIFO ordering, drop-oldest overflow, epoch filtering, and worker lifecycle.
import time

import numpy as np

from conftest import FakeSpeakerMedia

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


# ---------------------------------------------------------------------------
# Fix 1: played counter must NOT fire when speaker submit returns False
# ---------------------------------------------------------------------------


def test_played_counter_not_incremented_on_speaker_stall() -> None:
    """_playback_loop must only call record_audio_output_played on the success path.

    Drive one full iteration of _playback_loop with a speaker that returns False
    and is immediately 'stalled' (stalled() returns True).  The played counter
    must stay 0.
    """
    import asyncio

    from reachy_openai_realtime.realtime import RealtimeRobotSession
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    class CountingStatus(RuntimeStatus):
        def __init__(self) -> None:
            super().__init__()
            self.played_calls = 0

        def record_audio_output_played(self) -> None:
            self.played_calls += 1
            super().record_audio_output_played()

    class AlwaysStallingSpeaker:
        """submit always returns False; stalled() always True."""

        frames_total = 0

        def submit(self, pcm: np.ndarray, duration_ms: float, received_at: float,
                   timeout_seconds: float) -> bool:
            return False

        def stalled(self, threshold_seconds: float) -> bool:
            return True

        def flush(self) -> None:
            pass

    class OneChunkStopEvent:
        """Stop after _restart_media_pipeline is called (pipeline restarted once)."""

        def __init__(self) -> None:
            self._done = False

        def is_set(self) -> bool:
            return self._done

    stop = OneChunkStopEvent()

    class FakeMediaWithRestart:
        """Media that stops the loop once the pipeline restarts."""

        def get_output_audio_samplerate(self) -> int:
            return 24_000

        def stop_playing(self) -> None:
            pass

        def stop_recording(self) -> None:
            pass

        def start_recording(self) -> None:
            pass

        def start_playing(self) -> None:
            stop._done = True  # pipeline restarted: stop the loop

    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": FakeMediaWithRestart()})()
    session.motion = type("M", (), {"set_speaking_enabled": lambda self, v: None})()
    session.status = CountingStatus()
    session._speaker = AlwaysStallingSpeaker()
    session._playback = PlaybackBuffer()
    session._playback_io_lock = asyncio.Lock()
    session._playback_started_at = None
    session._playback_pushed_ms = 0.0
    session.connection_epoch = 1
    session.fsm = type("FSM", (), {"state": None, "generation_active": lambda self: False})()
    session._speaker_busy_until = time.monotonic() - 1.0
    session.config = type("Cfg", (), {"output_rate": 24_000})()

    # Push one chunk so the loop has something to process
    session._playback.push(chunk(80.0, epoch=1))

    asyncio.run(session._playback_loop(stop))

    assert session.status.played_calls == 0, (
        "played counter must not increment when speaker submit returns False (stall path)"
    )


def test_played_counter_increments_on_success_path() -> None:
    """record_audio_output_played fires once per successfully submitted chunk."""
    import asyncio

    from reachy_openai_realtime.realtime import RealtimeRobotSession
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    class CountingStatus(RuntimeStatus):
        def __init__(self) -> None:
            super().__init__()
            self.played_calls = 0

        def record_audio_output_played(self) -> None:
            self.played_calls += 1
            super().record_audio_output_played()

    class ImmediateAcceptSpeaker:
        """submit returns True immediately; on_write callback not wired."""

        frames_total = 0

        def submit(self, pcm: np.ndarray, duration_ms: float, received_at: float,
                   timeout_seconds: float) -> bool:
            return True

        def stalled(self, threshold_seconds: float) -> bool:
            return False

    class OneChunkStopEvent:
        def __init__(self) -> None:
            self._done = False
            self.count = 0

        def is_set(self) -> bool:
            if self._done:
                return True
            self.count += 1
            # Stop after we've popped the one chunk (second iteration hits empty buffer)
            return self._done

    stop = OneChunkStopEvent()

    class StopAfterFirstSuccessMedia:
        def get_output_audio_samplerate(self) -> int:
            return 24_000

    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.robot = type("Robot", (), {"media": StopAfterFirstSuccessMedia()})()
    session.motion = type("M", (), {"set_speaking_enabled": lambda self, v: None})()
    session.status = CountingStatus()
    session._speaker = ImmediateAcceptSpeaker()
    session._playback = PlaybackBuffer()
    session._playback_io_lock = asyncio.Lock()
    session._playback_started_at = None
    session._playback_pushed_ms = 0.0
    session.connection_epoch = 1
    session.fsm = type("FSM", (), {"state": None, "generation_active": lambda self: False})()
    session._speaker_busy_until = time.monotonic() - 1.0
    session.config = type("Cfg", (), {"output_rate": 24_000})()

    # Push one chunk; stop after it's been processed by setting stop event flag
    session._playback.push(chunk(80.0, epoch=1))
    # Rig the stop event to stop after a few iterations (one chunk processed)
    calls = [0]

    def counting_is_set() -> bool:
        calls[0] += 1
        # Let first iteration run; stop on the second timeout (empty buffer)
        return calls[0] > 3

    stop.is_set = counting_is_set  # type: ignore[method-assign]

    asyncio.run(session._playback_loop(stop))

    assert session.status.played_calls == 1, (
        "played counter must increment exactly once for one successful submit"
    )


# ---------------------------------------------------------------------------
# Fix 2: _handle_playback_overrun must reset _speaker_busy_until
# ---------------------------------------------------------------------------


def test_overrun_handler_resets_speaker_busy_until() -> None:
    """After _handle_playback_overrun, _speaker_busy_until must not be in the future."""
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
    session._current_response_id = "resp_overrun2"
    session._interrupted_response_ids = RecentIds()
    # Simulate a speaker busy pushed far into the future (audio flood)
    session._speaker_busy_until = time.monotonic() + 60.0

    asyncio.run(session._handle_playback_overrun(900.0))

    assert session._speaker_busy_until <= time.monotonic(), (
        "_speaker_busy_until must be reset after overrun so _assistant_audio_active() returns False"
    )


# ---------------------------------------------------------------------------
# Fix 3: SpeakerWorker.start() must raise on double-start
# ---------------------------------------------------------------------------


def test_speaker_worker_double_start_raises() -> None:
    """Calling start() a second time must raise RuntimeError and not orphan a thread."""
    import pytest

    worker = SpeakerWorker(FakeSpeakerMedia())
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            worker.start()
    finally:
        worker.close()


# ---------------------------------------------------------------------------
# Existing overrun integration test
# ---------------------------------------------------------------------------


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
