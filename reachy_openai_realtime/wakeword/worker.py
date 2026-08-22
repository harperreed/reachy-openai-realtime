# ABOUTME: Wake-word worker thread — reads the mic subscription, runs the detector,
# ABOUTME: fills the pre-roll ring buffer, emits debounced WakeEvents (spec §12, §14, §38).
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..audio.fanout import AudioSubscription, prepare_wake_audio
from .base import WakeWordDetector
from .buffer import AudioRingBuffer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WakeEvent:
    id: str
    detected_at: float
    phrase: str
    score: float


class WakeWordWorker:
    """One thread: pop frame -> ring-buffer append -> detector.process -> maybe emit.
    A monitor thread restarts the detector (the worker only) if a classify call
    stalls past `stall_seconds` — the media pipeline is never touched here."""

    def __init__(
        self,
        subscription: AudioSubscription,
        detector: WakeWordDetector,
        ring_buffer: AudioRingBuffer,
        on_wake: Callable[[WakeEvent], None],
        *,
        debounce_seconds: float = 2.0,
        stall_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self._subscription = subscription
        self._detector = detector
        self._ring_buffer = ring_buffer
        self._on_wake = on_wake
        self._debounce_seconds = debounce_seconds
        self._stall_seconds = stall_seconds
        self._clock = clock
        self._on_event = on_event
        self._stop = threading.Event()
        self._restart_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._last_activity = clock()
        self._last_wake_at = 0.0
        self._event_seq = 0
        self._last_dropped = 0
        self.frames_total = 0
        self.wake_count = 0
        self.restart_count = 0
        self.backend_error_count = 0

    def _emit(self, event: str, **fields: object) -> None:
        """Best-effort structured-log emit (spec §27). Observability must never
        take down the worker, so a raising sink is swallowed."""
        if self._on_event is None:
            return
        try:
            self._on_event(event, **fields)
        except Exception:  # a broken log sink must not stop wake detection
            logger.exception("wake event sink raised")

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("WakeWordWorker already started")
        self._thread = threading.Thread(target=self._run, name="wake-word", daemon=True)
        self._thread.start()
        self._monitor = threading.Thread(target=self._watchdog, name="wake-watchdog", daemon=True)
        self._monitor.start()

    def close(self) -> None:
        self._stop.set()
        for thread in (self._thread, self._monitor):
            if thread is not None:
                thread.join(timeout=3.0)
        self._thread = None
        self._monitor = None
        try:
            self._detector.close()
        except Exception:
            logger.exception("error closing wake detector")

    def seconds_since_process(self) -> float:
        return self._clock() - self._last_activity

    def _run(self) -> None:
        self._emit("wake.model_loading")
        try:
            self._detector.start()
        except Exception:
            logger.exception("wake detector failed to start")
            self.backend_error_count += 1
            self._emit("wake.model_error", phase="start")
            return
        self._emit("wake.model_ready")
        self._last_activity = self._clock()
        while not self._stop.is_set():
            if self._restart_requested.is_set():
                self._restart_requested.clear()
                self._rebuild_detector()
            frame = self._subscription.pop(0.25)
            if frame is not None:
                self._ring_buffer.append(frame)
                self.frames_total += 1
                self._note_dropped_frames()
                self._classify(frame)
            self._last_activity = self._clock()

    def _note_dropped_frames(self) -> None:
        # The mic subscription is drop-oldest (Task 3). A rising drop count means
        # inference fell behind capture; surface it (spec §27 wake.buffer_overflow)
        # but never block — the dropped audio is already gone.
        dropped = self._subscription.dropped_frames
        if dropped > self._last_dropped:
            self._emit("wake.buffer_overflow", dropped=dropped - self._last_dropped, total_dropped=dropped)
            self._last_dropped = dropped

    def _classify(self, frame) -> None:
        try:
            pcm16 = prepare_wake_audio(frame, self._detector.required_sample_rate).tobytes()
            detection = self._detector.process(pcm16)
        except Exception:
            logger.exception("wake classify failed")
            self.backend_error_count += 1
            self._emit("wake.model_error", phase="classify")
            return
        if detection is None:
            return
        now = self._clock()
        if now - self._last_wake_at < self._debounce_seconds:
            self._emit("wake.debounced", score=round(detection.score, 3))
            return
        self._last_wake_at = now
        self._event_seq += 1
        self._detector.reset()
        self.wake_count += 1
        event = WakeEvent(
            id=f"wake-{self._event_seq}",
            detected_at=detection.detected_at,
            phrase=detection.phrase,
            score=detection.score,
        )
        try:
            self._on_wake(event)
        except Exception:
            logger.exception("wake callback raised")

    def _rebuild_detector(self) -> None:
        try:
            self._detector.close()
        except Exception:
            logger.exception("error closing wake detector during restart")
        self._emit("wake.model_loading", reason="restart")
        try:
            self._detector.start()
            self.restart_count += 1
            self._emit("wake.worker_restarted", restart_count=self.restart_count)
        except Exception:
            logger.exception("wake detector failed to restart")
            self.backend_error_count += 1
            self._emit("wake.model_error", phase="restart")

    def _watchdog(self) -> None:
        while not self._stop.wait(self._stall_seconds / 2):
            if self.seconds_since_process() > self._stall_seconds and not self._restart_requested.is_set():
                stalled = self.seconds_since_process()
                logger.warning("wake worker stalled %.1fs; restarting detector", stalled)
                self._emit("wake.worker_stalled", stalled_seconds=round(stalled, 3))
                self._restart_requested.set()
                try:
                    self._detector.close()  # interrupt a blocked classify recv
                except Exception:
                    logger.exception("error interrupting stalled detector")
