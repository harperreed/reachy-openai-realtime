# ABOUTME: Audio fan-out — one CaptureWorker mic reader feeding many bounded,
# ABOUTME: drop-oldest subscriptions, plus the shared wake-audio converter (spec §9, §10).
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

from ..dsp import float32_to_pcm16, resample_linear, select_mono_float32


@dataclass(frozen=True)
class AudioFrame:
    samples: np.ndarray
    sample_rate: int
    captured_at: float


class AudioSubscription:
    """A bounded, drop-oldest queue of AudioFrames for one consumer. If the
    consumer falls behind, the oldest frames are dropped so capture never
    blocks (spec §9)."""

    def __init__(self, name: str, *, max_buffer_ms: float = 500.0) -> None:
        self.name = name
        self._max_buffer_ms = max_buffer_ms
        self._frames: deque[AudioFrame] = deque()
        self._buffered_ms = 0.0
        self._lock = threading.Lock()
        self._available = threading.Event()
        self.dropped_frames = 0

    def _offer(self, frame: AudioFrame) -> None:
        with self._lock:
            self._frames.append(frame)
            self._buffered_ms += _frame_ms(frame)
            while self._buffered_ms > self._max_buffer_ms and len(self._frames) > 1:
                dropped = self._frames.popleft()
                self._buffered_ms -= _frame_ms(dropped)
                self.dropped_frames += 1
            self._available.set()

    def pop(self, timeout_seconds: float) -> AudioFrame | None:
        if not self._available.wait(timeout_seconds):
            return None
        with self._lock:
            if not self._frames:
                self._available.clear()
                return None
            frame = self._frames.popleft()
            self._buffered_ms -= _frame_ms(frame)
            if not self._frames:
                self._available.clear()
            return frame


def _frame_ms(frame: AudioFrame) -> float:
    return len(frame.samples) / frame.sample_rate * 1000.0


def prepare_wake_audio(frame: AudioFrame, target_rate: int = 24_000) -> np.ndarray:
    """Convert one captured frame to the wake model's expected format:
    mono, resampled to target_rate, clamped signed int16. Stateless — the
    rolling-window accumulation lives in the detector (spec §10)."""
    mono, _selected_channel, _channel_levels = select_mono_float32(frame.samples)
    if frame.sample_rate != target_rate:
        mono = resample_linear(mono, frame.sample_rate, target_rate)
    return float32_to_pcm16(mono)
