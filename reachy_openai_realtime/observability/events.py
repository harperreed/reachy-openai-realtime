# ABOUTME: JSONL flight recorder for runtime events, with secret redaction and
# ABOUTME: size-based rotation. Canonical home of log-redaction helpers.
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

logger = logging.getLogger(__name__)

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


def redact_secrets(text: str) -> str:
    """Mask OpenAI-style API keys anywhere in a string."""
    return _SECRET_PATTERN.sub("sk-***", text)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


class EventRecorder:
    """Append-only JSONL event log. Thread-safe; recording never raises."""

    def __init__(self, path: Path, *, max_bytes: int = 5_000_000, keep_files: int = 2) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._keep_files = keep_files
        self._lock = threading.Lock()
        self._file: TextIO | None = None
        self._epoch_provider: Callable[[], int] | None = None
        self._state_provider: Callable[[], str] | None = None

    def set_context_providers(
        self,
        *,
        epoch: Callable[[], int] | None = None,
        state: Callable[[], str] | None = None,
    ) -> None:
        self._epoch_provider = epoch
        self._state_provider = state

    def record(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        for key, provider in (
            ("connection_epoch", self._epoch_provider),
            ("session_state", self._state_provider),
        ):
            if provider is None:
                continue
            try:
                entry[key] = provider()
            except Exception:
                # Context is best-effort; a half-initialized session must not
                # stop the flight recorder.
                continue
        entry.update(_redact_value(fields))
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._write_locked(line)
            except OSError as exc:
                logger.debug("event recorder write failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None

    def _write_locked(self, line: str) -> None:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")
        self._file.write(line + "\n")
        self._file.flush()
        if self._file.tell() >= self._max_bytes:
            self._rotate_locked()

    def _rotate_locked(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        for index in range(self._keep_files - 1, 0, -1):
            source = self._path.with_name(f"{self._path.name}.{index}")
            if source.exists():
                source.replace(self._path.with_name(f"{self._path.name}.{index + 1}"))
        overflow = self._path.with_name(f"{self._path.name}.{self._keep_files + 1}")
        if overflow.exists():
            overflow.unlink()
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))
        # Open a fresh log file immediately so the primary path always exists
        # after rotation — important for test assertions and external monitors.
        self._file = self._path.open("a", encoding="utf-8")
