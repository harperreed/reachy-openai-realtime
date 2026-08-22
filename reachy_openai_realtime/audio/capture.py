# ABOUTME: Dedicated mic-capture thread with fan-out to bounded per-consumer
# ABOUTME: subscriptions, plus the stall-recovery ladder (spec §6, §9).
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .fanout import AudioFrame, AudioSubscription

logger = logging.getLogger(__name__)


class AudioPipelineStalled(RuntimeError):
    """Mic recovery exhausted; the whole app session must be rebuilt."""


class CaptureWorker:
    """Continuously drains media.get_audio_sample() so the SDK buffer never
    grows unbounded (reachy_mini issue #436), regardless of session state, and
    fans each frame out to every subscriber (spec §9). One mic reader, many
    consumers, for the whole app lifetime."""

    def __init__(self, media: Any, *, max_buffer_ms: float = 500.0) -> None:
        self._media = media
        self._default_max_buffer_ms = max_buffer_ms
        self._subscribers: dict[str, AudioSubscription] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_rate = 16_000
        self.last_frame_at = time.monotonic()
        self.frames_total = 0

    def subscribe(self, name: str, *, max_buffer_ms: float | None = None) -> AudioSubscription:
        sub = AudioSubscription(name, max_buffer_ms=max_buffer_ms or self._default_max_buffer_ms)
        with self._lock:
            self._subscribers[name] = sub
        return sub

    def unsubscribe(self, name: str) -> None:
        with self._lock:
            self._subscribers.pop(name, None)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CaptureWorker already started")
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()
        self._started.wait()  # block until samplerate read so callers can subscribe before first frame

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def frame_age_seconds(self) -> float:
        return time.monotonic() - self.last_frame_at

    def _run(self) -> None:
        try:
            self._sample_rate = int(self._media.get_input_audio_samplerate())
        except Exception:
            logger.exception("could not read input samplerate; assuming 16 kHz")
        self._started.set()  # unblock start() so callers can subscribe before first frame is offered
        time.sleep(0.005)  # yield to main thread so subscriptions can be registered before first frame
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
            self.frames_total += 1  # single writer (capture thread); reads are advisory
            audio_frame = AudioFrame(
                samples=frame, sample_rate=self._sample_rate, captured_at=self.last_frame_at
            )
            with self._lock:
                subscribers = tuple(self._subscribers.values())
            for sub in subscribers:
                sub._offer(audio_frame)


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
