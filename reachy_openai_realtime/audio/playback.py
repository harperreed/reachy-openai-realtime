# ABOUTME: Latency-bounded playback jitter buffer (spec §7) and the dedicated
# ABOUTME: speaker-write thread. Freshness beats completeness: oldest audio drops first.
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

TARGET_BUFFER_MS = 200.0
MAX_BUFFER_MS = 500.0
HARD_MAX_BUFFER_MS = 1000.0
READY_BEEP_FREQUENCY_HZ = 880.0
READY_BEEP_DURATION_MS = 160.0
READY_BEEP_AMPLITUDE = 0.15


def make_ready_beep(sample_rate: int) -> np.ndarray:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    sample_count = round(sample_rate * READY_BEEP_DURATION_MS / 1_000.0)
    positions = np.arange(sample_count, dtype=np.float32) / np.float32(sample_rate)
    return (READY_BEEP_AMPLITUDE * np.sin(2.0 * np.pi * READY_BEEP_FREQUENCY_HZ * positions)).astype(
        np.float32
    )


class SpeakerWriteReceipt:
    def __init__(self) -> None:
        self._done = threading.Event()
        self._succeeded = False

    def resolve(self, *, succeeded: bool) -> None:
        self._succeeded = succeeded
        self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def succeeded(self) -> bool:
        return self._done.is_set() and self._succeeded


@dataclass
class _SpeakerWrite:
    pcm: np.ndarray
    duration_ms: float
    received_at: float
    receipt: SpeakerWriteReceipt | None = None


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
        self._inbox: queue.Queue[_SpeakerWrite] = queue.Queue(maxsize=inbox_max)
        self._on_write = on_write
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_write_at = time.monotonic()
        self.frames_total = 0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("SpeakerWorker already started")
        self._thread = threading.Thread(target=self._run, name="audio-speaker", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def submit(self, pcm: np.ndarray, duration_ms: float, received_at: float, timeout_seconds: float) -> bool:
        return self._submit(_SpeakerWrite(pcm, duration_ms, received_at), timeout_seconds)

    def submit_tracked(
        self,
        pcm: np.ndarray,
        duration_ms: float,
        received_at: float,
        timeout_seconds: float,
    ) -> SpeakerWriteReceipt | None:
        receipt = SpeakerWriteReceipt()
        write = _SpeakerWrite(pcm, duration_ms, received_at, receipt)
        return receipt if self._submit(write, timeout_seconds) else None

    def _submit(self, write: _SpeakerWrite, timeout_seconds: float) -> bool:
        try:
            self._inbox.put(write, timeout=timeout_seconds)
            return True
        except queue.Full:
            return False

    def flush(self) -> None:
        while True:
            try:
                write = self._inbox.get_nowait()
            except queue.Empty:
                return
            if write.receipt is not None:
                write.receipt.resolve(succeeded=False)

    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def stalled(self, threshold_seconds: float) -> bool:
        return self._inbox.full() and time.monotonic() - self.last_write_at > threshold_seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                write = self._inbox.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._media.push_audio_sample(write.pcm)
            except Exception:
                logger.exception("speaker write failed")
                if write.receipt is not None:
                    write.receipt.resolve(succeeded=False)
                continue
            self.last_write_at = time.monotonic()
            self.frames_total += 1
            if write.receipt is not None:
                write.receipt.resolve(succeeded=True)
            if self._on_write is not None:
                try:
                    self._on_write(write.duration_ms, write.received_at)
                except Exception:
                    logger.exception("on_write callback failed")
