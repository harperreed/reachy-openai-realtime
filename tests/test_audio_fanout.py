import time

import numpy as np

from reachy_openai_realtime.audio.capture import CaptureWorker
from reachy_openai_realtime.audio.fanout import AudioFrame, prepare_wake_audio


class FakeMedia:
    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def get_input_audio_samplerate(self):
        return 16_000

    def get_audio_sample(self):
        if self._i >= len(self._frames):
            return None
        frame = self._frames[self._i]
        self._i += 1
        return frame


def _pop_n(sub, n, timeout=2.0):
    out = []
    deadline = time.monotonic() + timeout
    while len(out) < n and time.monotonic() < deadline:
        frame = sub.pop(0.1)
        if frame is not None:
            out.append(frame)
    return out


def test_every_subscriber_receives_every_frame():
    frames = [np.full(160, i, dtype=np.int16) for i in range(1, 4)]
    worker = CaptureWorker(FakeMedia(frames))
    worker.start()
    try:
        a = worker.subscribe("a")
        b = worker.subscribe("b")
        got_a = _pop_n(a, 3)
        got_b = _pop_n(b, 3)
    finally:
        worker.close()
    assert [int(f.samples[0]) for f in got_a] == [1, 2, 3]
    assert [int(f.samples[0]) for f in got_b] == [1, 2, 3]
    assert all(isinstance(f, AudioFrame) and f.sample_rate == 16_000 for f in got_a)


def test_slow_subscriber_drops_oldest_without_blocking_others():
    frames = [np.full(16_000, i % 127, dtype=np.int16) for i in range(50)]  # 1s each at 16kHz
    worker = CaptureWorker(FakeMedia(frames))
    worker.start()
    try:
        slow = worker.subscribe("slow", max_buffer_ms=500)  # holds < 1 frame
        time.sleep(0.3)
        drained = []
        while True:
            frame = slow.pop(0.05)
            if frame is None:
                break
            drained.append(frame)
    finally:
        worker.close()
    assert slow.dropped_frames > 0  # oldest frames were dropped, not buffered unbounded


def test_prepare_wake_audio_resamples_to_target_and_returns_int16():
    samples = (np.sin(np.linspace(0, 20, 320)) * 20_000).astype(np.int16)
    frame = AudioFrame(samples=samples, sample_rate=16_000, captured_at=0.0)
    out = prepare_wake_audio(frame, target_rate=24_000)
    assert out.dtype == np.int16
    assert out.shape[0] == 480  # 320 @ 16k -> 480 @ 24k
