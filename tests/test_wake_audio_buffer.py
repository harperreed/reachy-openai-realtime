import numpy as np

from reachy_openai_realtime.audio.fanout import AudioFrame
from reachy_openai_realtime.wakeword.buffer import AudioRingBuffer


def _frame(captured_at):
    return AudioFrame(samples=np.zeros(160, dtype=np.int16), sample_rate=16_000, captured_at=captured_at)


def test_since_returns_frames_at_or_after_timestamp():
    buf = AudioRingBuffer(history_seconds=10.0)
    for t in (1.0, 2.0, 3.0, 4.0):
        buf.append(_frame(t))
    got = buf.since(2.5)
    assert [f.captured_at for f in got] == [3.0, 4.0]


def test_append_trims_frames_older_than_history():
    buf = AudioRingBuffer(history_seconds=2.0)
    buf.append(_frame(1.0))
    buf.append(_frame(2.0))
    buf.append(_frame(4.0))  # now=4.0 -> cutoff 2.0, drops the 1.0 frame
    remaining = [f.captured_at for f in buf.since(0.0)]
    assert 1.0 not in remaining
    assert remaining == [2.0, 4.0]


def test_clear_empties_the_buffer():
    buf = AudioRingBuffer()
    buf.append(_frame(1.0))
    buf.clear()
    assert buf.since(0.0) == []
