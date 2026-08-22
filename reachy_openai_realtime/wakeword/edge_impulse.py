# ABOUTME: Edge Impulse wake-word detector — maintains a rolling PCM16 window and
# ABOUTME: classifies every slice_size new samples via the vendored runner (spec §7).
from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from .base import WakeWordDetection
from .eim_runner import EimRunner

_PHRASE = "hey reachy"
_LABEL = "hey_reachy"


class EdgeImpulseWakeWordDetector:
    def __init__(
        self,
        model_path: str,
        threshold: float = 0.70,
        *,
        runner_factory: Callable[[str], EimRunner] = EimRunner,
    ) -> None:
        self._model_path = model_path
        self._threshold = threshold
        self._runner_factory = runner_factory
        self._runner: EimRunner | None = None
        self._window = np.empty(0, dtype=np.int16)
        self._samples_since_classify = 0
        self._window_size = 48_000
        self._slice_size = 12_000
        self._frequency = 24_000

    @property
    def required_sample_rate(self) -> int:
        return self._frequency

    def start(self) -> None:
        runner = self._runner_factory(self._model_path)
        params = runner.start()
        self._runner = runner
        self._frequency = params.frequency
        self._window_size = params.input_features_count
        self._slice_size = params.slice_size

    def process(self, pcm16: bytes) -> WakeWordDetection | None:
        if self._runner is None:
            raise RuntimeError("detector not started")
        incoming = np.frombuffer(pcm16, dtype=np.int16)
        if incoming.size == 0:
            return None
        self._window = np.concatenate([self._window, incoming])[-self._window_size :]
        self._samples_since_classify += incoming.size
        if self._window.size < self._window_size or self._samples_since_classify < self._slice_size:
            return None
        self._samples_since_classify = 0
        try:
            scores = self._runner.classify(self._window.tolist())
        except Exception:  # noqa: BLE001 — never crash the app on a classifier error (spec §7)
            return None
        score = float(scores.get(_LABEL, 0.0))
        if score >= self._threshold:
            return WakeWordDetection(phrase=_PHRASE, score=score, detected_at=time.monotonic())
        return None

    def reset(self) -> None:
        self._window = np.empty(0, dtype=np.int16)
        self._samples_since_classify = 0

    def close(self) -> None:
        if self._runner is not None:
            self._runner.close()
            self._runner = None
