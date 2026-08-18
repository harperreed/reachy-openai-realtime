# ABOUTME: Latency-bounded playback jitter buffer (spec §7) and the dedicated
# ABOUTME: speaker-write thread. Freshness beats completeness: oldest audio drops first.
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)

TARGET_BUFFER_MS = 200.0
MAX_BUFFER_MS = 500.0
HARD_MAX_BUFFER_MS = 1000.0


@dataclass
class PlaybackChunk:
    epoch: int
    response_id: str
    pcm: np.ndarray
    duration_ms: float
    received_at: float


@dataclass
class PushResult:
    dropped_ms: float
    overrun: bool


class PlaybackBuffer:
    """Time-accounted FIFO. Thread-safe: the event loop pushes, a to_thread
    consumer pops, and the status API reads queued_ms."""

    def __init__(
        self,
        *,
        target_ms: float = TARGET_BUFFER_MS,
        max_ms: float = MAX_BUFFER_MS,
        hard_max_ms: float = HARD_MAX_BUFFER_MS,
    ) -> None:
        self.target_ms = target_ms
        self.max_ms = max_ms
        self.hard_max_ms = hard_max_ms
        self._chunks: deque[PlaybackChunk] = deque()
        self._queued_ms = 0.0
        self._lock = threading.Lock()
        self._available = threading.Event()

    def push(self, chunk: PlaybackChunk) -> PushResult:
        dropped_ms = 0.0
        with self._lock:
            self._chunks.append(chunk)
            self._queued_ms += chunk.duration_ms
            while self._queued_ms > self.max_ms and len(self._chunks) > 1:
                dropped = self._chunks.popleft()
                self._queued_ms -= dropped.duration_ms
                dropped_ms += dropped.duration_ms
            overrun = self._queued_ms >= self.hard_max_ms
            self._available.set()
        return PushResult(dropped_ms=dropped_ms, overrun=overrun)

    def pop_wait(self, timeout_seconds: float, current_epoch: int) -> PlaybackChunk | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._available.wait(remaining):
                return None
            with self._lock:
                while self._chunks:
                    chunk = self._chunks.popleft()
                    self._queued_ms -= chunk.duration_ms
                    if chunk.epoch != current_epoch:
                        continue  # stale connection audio must never play (spec §4)
                    if not self._chunks:
                        self._available.clear()
                    return chunk
                self._queued_ms = 0.0
                self._available.clear()

    def queued_ms(self) -> float:
        with self._lock:
            return self._queued_ms

    def clear(self) -> float:
        with self._lock:
            dropped = self._queued_ms
            self._chunks.clear()
            self._queued_ms = 0.0
            self._available.clear()
            return dropped


class SpeakerWorker:
    """Owns all push_audio_sample calls so a wedged ALSA write can never block
    the event loop. Never touches stop_playing (shared Wireless pipeline)."""

    def __init__(
        self,
        media: Any,
        *,
        inbox_max: int = 4,
        on_write: Callable[[float, float], None] | None = None,
    ) -> None:
        self._media = media
        self._inbox: queue.Queue[tuple[np.ndarray, float, float]] = queue.Queue(maxsize=inbox_max)
        self._on_write = on_write
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_write_at = time.monotonic()
        self.frames_total = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-speaker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def submit(self, pcm: np.ndarray, duration_ms: float, received_at: float, timeout_seconds: float) -> bool:
        try:
            self._inbox.put((pcm, duration_ms, received_at), timeout=timeout_seconds)
            return True
        except queue.Full:
            return False

    def flush(self) -> None:
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                return

    def stalled(self, threshold_seconds: float) -> bool:
        return self._inbox.full() and time.monotonic() - self.last_write_at > threshold_seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                pcm, duration_ms, received_at = self._inbox.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._media.push_audio_sample(pcm)
            except Exception:
                logger.exception("speaker write failed")
                continue
            self.last_write_at = time.monotonic()
            self.frames_total += 1
            if self._on_write is not None:
                try:
                    self._on_write(duration_ms, received_at)
                except Exception:
                    logger.exception("on_write callback failed")
