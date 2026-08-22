# ABOUTME: Time-bounded ring buffer of recent AudioFrames for wake pre-roll — the
# ABOUTME: ~250-500ms of audio before the wake word. Memory-only, never persisted (spec §13).
from __future__ import annotations

import threading
from collections import deque

from ..audio.fanout import AudioFrame


class AudioRingBuffer:
    """Holds the most recent `history_seconds` of captured audio so the pre-roll
    before a wake word can be recovered. Thread-safe; never written to disk."""

    def __init__(self, *, history_seconds: float = 4.0) -> None:
        self._history_seconds = history_seconds
        self._frames: deque[AudioFrame] = deque()
        self._lock = threading.Lock()

    def append(self, frame: AudioFrame) -> None:
        with self._lock:
            self._frames.append(frame)
            cutoff = frame.captured_at - self._history_seconds
            while self._frames and self._frames[0].captured_at < cutoff:
                self._frames.popleft()

    def since(self, timestamp: float) -> list[AudioFrame]:
        with self._lock:
            return [f for f in self._frames if f.captured_at >= timestamp]

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
