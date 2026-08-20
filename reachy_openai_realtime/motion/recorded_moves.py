# ABOUTME: RecordedMoveCatalog wraps the reachy_mini SDK's HuggingFace recorded-move
# ABOUTME: libraries with background loading, validation, and graceful degradation.
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"
DANCES_DATASET = "pollen-robotics/reachy-mini-dances-library"

# Names flow into session instructions; anything else is dropped (injection hygiene).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 _\-]{1,64}$")


def _default_loader(dataset: str) -> Any:
    from reachy_mini.motion.recorded_move import RecordedMoves

    return RecordedMoves(dataset)


class RecordedMoveCatalog:
    def __init__(self, dataset: str, *, loader: Callable[[str], Any] | None = None) -> None:
        self.dataset = dataset
        self._loader = loader or _default_loader
        self._lock = threading.Lock()
        self._state = "loading"
        self._moves: Any | None = None
        self._names: list[str] = []
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def load_async(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._load, name=f"recorded-moves-{self.dataset.rsplit('/', 1)[-1]}", daemon=True
            )
        self._thread.start()

    def _load(self) -> None:
        try:
            moves = self._loader(self.dataset)
            raw_names = list(moves.list_moves())
        except Exception:
            logger.warning("Recorded-move catalog unavailable: %s", self.dataset, exc_info=True)
            with self._lock:
                self._state = "unavailable"
            self._done.set()
            return
        safe = sorted(name for name in raw_names if _SAFE_NAME.match(name))
        dropped = sorted(set(raw_names) - set(safe))
        if dropped:
            logger.warning("Dropped %d unsanitary move names from %s: %s", len(dropped), self.dataset, dropped)
        with self._lock:
            self._moves = moves
            self._names = safe
            self._state = "ready"
        self._done.set()

    def wait_ready(self, timeout: float) -> bool:
        self._done.wait(timeout=timeout)
        return self.available

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def available(self) -> bool:
        return self.state == "ready"

    def names(self) -> list[str]:
        with self._lock:
            return list(self._names)

    def get(self, name: str) -> Any:
        with self._lock:
            if self._state != "ready" or self._moves is None:
                raise RuntimeError(f"catalog not ready: {self.dataset}")
            if name not in self._names:
                raise ValueError(f"unknown move {name!r} in {self.dataset}")
            moves = self._moves
        return moves.get(name)
