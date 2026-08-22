# ABOUTME: Unit tests for CaptureWorker and AudioRecoveryLadder. Verifies
# ABOUTME: buffering, drop-oldest eviction, stall detection, and ladder escalation.
import queue
import threading
import time

import numpy as np
import pytest

from reachy_openai_realtime.audio.capture import (
    AudioPipelineStalled,
    AudioRecoveryLadder,
    CaptureWorker,
)


class ScriptedMedia:
    """Feed-controlled fake media; get_audio_sample drains one queued frame."""

    def __init__(self) -> None:
        self._frames: queue.Queue[np.ndarray] = queue.Queue()

    def feed(self, frame: np.ndarray) -> None:
        self._frames.put(frame)

    def get_audio_sample(self) -> np.ndarray | None:
        try:
            return self._frames.get_nowait()
        except queue.Empty:
            return None

    def get_input_audio_samplerate(self) -> int:
        return 16_000


def frame_of_ms(ms: float) -> np.ndarray:
    samples = int(16_000 * ms / 1000.0)
    return np.zeros((samples, 2), dtype=np.float32)


def test_pop_returns_fed_frames_in_order() -> None:
    media = ScriptedMedia()
    worker = CaptureWorker(media)
    worker.start()
    sub = worker.subscribe("test")
    try:
        first = frame_of_ms(20.0)
        first[0, 0] = 1.0
        media.feed(first)
        media.feed(frame_of_ms(20.0))
        popped = sub.pop(1.0)
        assert popped is not None
        assert popped.samples[0, 0] == 1.0
        assert sub.pop(1.0) is not None
        assert worker.frames_total == 2
    finally:
        worker.close()


def test_backlog_drops_oldest_beyond_budget() -> None:
    media = ScriptedMedia()
    worker = CaptureWorker(media, max_buffer_ms=100.0)
    worker.start()
    sub = worker.subscribe("test")
    try:
        for _ in range(50):
            media.feed(frame_of_ms(20.0))
        deadline = time.monotonic() + 2.0
        while worker.frames_total < 50 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.frames_total == 50
        assert sub.dropped_frames > 0
        remaining = 0
        while sub.pop(0.05) is not None:
            remaining += 1
        assert remaining <= 6  # ~100ms budget of 20ms frames (+1 in flight)
    finally:
        worker.close()


def test_pop_times_out_without_frames() -> None:
    worker = CaptureWorker(ScriptedMedia())
    worker.start()
    sub = worker.subscribe("test")
    try:
        started = time.monotonic()
        assert sub.pop(0.1) is None
        assert time.monotonic() - started < 1.0
    finally:
        worker.close()


def test_close_joins_thread() -> None:
    worker = CaptureWorker(ScriptedMedia())
    worker.start()
    before = threading.active_count()
    worker.close()
    assert threading.active_count() == before - 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def test_ladder_escalates_through_actions_with_cooldown() -> None:
    clock = FakeClock()
    ladder = AudioRecoveryLadder(stall_seconds=1.5, cooldown_seconds=3.0, clock=clock)
    assert ladder.next_action(0.2) is None
    assert ladder.next_action(2.0) == "restart_capture"
    assert ladder.next_action(2.5) is None  # cooldown
    clock.now += 4.0
    assert ladder.next_action(6.0) == "restart_media"
    clock.now += 4.0
    assert ladder.next_action(10.0) == "restart_session"
    clock.now += 4.0
    assert ladder.next_action(14.0) == "restart_session"  # stays at final rung


def test_ladder_resets_on_healthy_frames() -> None:
    clock = FakeClock()
    ladder = AudioRecoveryLadder(stall_seconds=1.5, cooldown_seconds=0.0, clock=clock)
    assert ladder.next_action(2.0) == "restart_capture"
    assert ladder.next_action(0.1) is None  # healthy → reset
    assert ladder.next_action(2.0) == "restart_capture"


def test_audio_pipeline_stalled_is_a_runtime_error() -> None:
    with pytest.raises(RuntimeError):
        raise AudioPipelineStalled("mic dead")


def test_start_twice_raises() -> None:
    worker = CaptureWorker(ScriptedMedia())
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            worker.start()
    finally:
        worker.close()


def test_start_after_close_is_allowed() -> None:
    worker = CaptureWorker(ScriptedMedia())
    worker.start()
    worker.close()
    worker.start()  # close() resets _thread; restart must stay legal
    worker.close()
