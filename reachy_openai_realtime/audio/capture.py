# ABOUTME: Dedicated mic-capture thread with a bounded drop-oldest frame buffer,
# ABOUTME: plus the stall-recovery ladder (spec §6). Isolates blocking SDK calls.
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class AudioPipelineStalled(RuntimeError):
    """Mic recovery exhausted; the whole app session must be rebuilt."""


class CaptureWorker:
    """Continuously drains media.get_audio_sample() so the SDK buffer never
    grows unbounded (reachy_mini issue #436), regardless of session state."""

    def __init__(self, media: Any, *, max_buffer_ms: float = 500.0) -> None:
        self._media = media
        self._max_buffer_ms = max_buffer_ms
        self._frames: deque[np.ndarray] = deque()
        self._buffered_ms = 0.0
        self._lock = threading.Lock()
        self._available = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_rate = 16_000
        self.last_frame_at = time.monotonic()
        self.frames_total = 0
        self.dropped_frames = 0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CaptureWorker already started")
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._available.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def frame_age_seconds(self) -> float:
        return time.monotonic() - self.last_frame_at

    def pop(self, timeout_seconds: float) -> np.ndarray | None:
        if not self._available.wait(timeout_seconds):
            return None
        with self._lock:
            if not self._frames:
                self._available.clear()
                return None
            frame = self._frames.popleft()
            self._buffered_ms -= self._frame_ms(frame)
            if not self._frames:
                self._available.clear()
            return frame

    def _run(self) -> None:
        try:
            self._sample_rate = int(self._media.get_input_audio_samplerate())
        except Exception:
            logger.exception("could not read input samplerate; assuming 16 kHz")
        while not self._stop.is_set():
            try:
                frame = self._media.get_audio_sample()
            except Exception:
                logger.exception("get_audio_sample failed")
                time.sleep(0.1)
                continue
            if frame is None or len(frame) == 0:
                time.sleep(0.005)  # SDK example polling cadence
                continue
            self.last_frame_at = time.monotonic()
            self.frames_total += 1  # written outside the lock: single writer (capture thread); reads are advisory
            with self._lock:
                self._frames.append(frame)
                self._buffered_ms += self._frame_ms(frame)
                while self._buffered_ms > self._max_buffer_ms and len(self._frames) > 1:
                    dropped = self._frames.popleft()
                    self._buffered_ms -= self._frame_ms(dropped)
                    self.dropped_frames += 1
                self._available.set()

    def _frame_ms(self, frame: np.ndarray) -> float:
        return len(frame) / self._sample_rate * 1000.0


class AudioRecoveryLadder:
    """Pure escalation logic for mic stalls: restart capture → restart media
    pipeline → restart app session. Never reboots Reachy (spec §6)."""

    ACTIONS = ("restart_capture", "restart_media", "restart_session")

    def __init__(
        self,
        *,
        stall_seconds: float = 1.75,
        cooldown_seconds: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stall_seconds = stall_seconds
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._attempt = 0
        self._last_action_at: float | None = None

    def next_action(self, frame_age_seconds: float) -> str | None:
        if frame_age_seconds < self._stall_seconds:
            self._attempt = 0
            self._last_action_at = None
            return None
        now = self._clock()
        if self._last_action_at is not None and now - self._last_action_at < self._cooldown_seconds:
            return None
        action = self.ACTIONS[min(self._attempt, len(self.ACTIONS) - 1)]
        self._attempt += 1
        self._last_action_at = now
        return action
