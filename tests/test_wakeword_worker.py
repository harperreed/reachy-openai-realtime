import threading
import time

import numpy as np

from reachy_openai_realtime.audio.fanout import AudioFrame, AudioSubscription
from reachy_openai_realtime.wakeword.base import WakeWordDetection
from reachy_openai_realtime.wakeword.buffer import AudioRingBuffer
from reachy_openai_realtime.wakeword.worker import WakeEvent, WakeWordWorker


class FakeDetector:
    """Fires on the Nth process() call, then keeps returning that detection so
    the test can prove debounce collapses the repeats into one WakeEvent."""

    def __init__(self, fire_after=2):
        self.required_sample_rate = 24_000
        self._calls = 0
        self._fire_after = fire_after
        self.started = False
        self.reset_count = 0
        self.closed = False

    def start(self):
        self.started = True

    def process(self, pcm16):
        self._calls += 1
        if self._calls >= self._fire_after:
            return WakeWordDetection(phrase="hey reachy", score=0.95, detected_at=time.monotonic())
        return None

    def reset(self):
        self.reset_count += 1

    def close(self):
        self.closed = True


def _frame(value=100):
    return AudioFrame(samples=np.full(160, value, dtype=np.int16), sample_rate=16_000, captured_at=time.monotonic())


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_worker_emits_one_debounced_wake_event_and_fills_ring_buffer():
    sub = AudioSubscription("wake", max_buffer_ms=5_000)
    ring = AudioRingBuffer()
    events = []
    lock = threading.Lock()

    def on_wake(event):
        with lock:
            events.append(event)

    worker = WakeWordWorker(sub, FakeDetector(fire_after=2), ring, on_wake, debounce_seconds=5.0)
    worker.start()
    try:
        for _ in range(10):
            sub._offer(_frame())
        assert _wait_until(lambda: len(events) >= 1)
        time.sleep(0.2)  # let the remaining frames drain; debounce must suppress repeats
    finally:
        worker.close()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, WakeEvent)
    assert event.phrase == "hey reachy"
    assert event.score == 0.95
    assert ring.since(0.0)  # every frame was appended to history
    assert worker.wake_count == 1


def test_worker_close_is_idempotent_and_closes_detector():
    sub = AudioSubscription("wake")
    detector = FakeDetector(fire_after=999)
    worker = WakeWordWorker(sub, detector, AudioRingBuffer(), lambda e: None)
    worker.start()
    worker.close()
    worker.close()
    assert detector.closed


def test_worker_emits_structured_events_to_sink():
    """The on_event sink receives the §27 model-lifecycle and debounce events."""
    sub = AudioSubscription("wake", max_buffer_ms=5_000)
    events = []
    lock = threading.Lock()

    def on_event(event, **fields):
        with lock:
            events.append((event, fields))

    def kinds():
        with lock:
            return [event for event, _ in events]

    # fire_after=1 keeps returning a detection: the first is a real wake, every
    # later frame lands inside the 5s debounce window and is suppressed.
    worker = WakeWordWorker(
        sub, FakeDetector(fire_after=1), AudioRingBuffer(), lambda e: None,
        debounce_seconds=5.0, on_event=on_event,
    )
    worker.start()
    try:
        assert _wait_until(lambda: "wake.model_ready" in kinds())
        for _ in range(6):
            sub._offer(_frame())
        assert _wait_until(lambda: "wake.debounced" in kinds())
    finally:
        worker.close()

    assert "wake.model_loading" in kinds()
    assert "wake.model_ready" in kinds()
