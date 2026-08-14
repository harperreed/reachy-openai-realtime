from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_COUNTER_KEYS = (
    "responses",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "input_text_tokens",
    "input_audio_tokens",
    "input_image_tokens",
    "cached_input_tokens",
    "cached_text_tokens",
    "cached_audio_tokens",
    "cached_image_tokens",
    "output_text_tokens",
    "output_audio_tokens",
)

# USD per one million tokens, from the official gpt-realtime-2.1 model page.
_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-realtime-2.1": {
        "input_text": 4.00,
        "input_audio": 32.00,
        "input_image": 5.00,
        "cached_text": 0.40,
        "cached_audio": 0.40,
        "cached_image": 0.50,
        "output_text": 24.00,
        "output_audio": 64.00,
    }
}

PRICING_SOURCE = "https://developers.openai.com/api/docs/models/gpt-realtime-2.1"
PRICING_AS_OF = "2026-08-14"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_counters() -> dict[str, int]:
    return {key: 0 for key in _COUNTER_KEYS}


def _get(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_usage(usage: object) -> dict[str, int]:
    """Normalize the billing-related fields from a Realtime response.done event."""
    input_details = _get(usage, "input_token_details", {}) or {}
    cached_details = _get(input_details, "cached_tokens_details", {}) or {}
    output_details = _get(usage, "output_token_details", {}) or {}
    return {
        "responses": 1,
        "total_tokens": _nonnegative_int(_get(usage, "total_tokens")),
        "input_tokens": _nonnegative_int(_get(usage, "input_tokens")),
        "output_tokens": _nonnegative_int(_get(usage, "output_tokens")),
        "input_text_tokens": _nonnegative_int(_get(input_details, "text_tokens")),
        "input_audio_tokens": _nonnegative_int(_get(input_details, "audio_tokens")),
        "input_image_tokens": _nonnegative_int(_get(input_details, "image_tokens")),
        "cached_input_tokens": _nonnegative_int(_get(input_details, "cached_tokens")),
        "cached_text_tokens": _nonnegative_int(_get(cached_details, "text_tokens")),
        "cached_audio_tokens": _nonnegative_int(_get(cached_details, "audio_tokens")),
        "cached_image_tokens": _nonnegative_int(_get(cached_details, "image_tokens")),
        "output_text_tokens": _nonnegative_int(_get(output_details, "text_tokens")),
        "output_audio_tokens": _nonnegative_int(_get(output_details, "audio_tokens")),
    }


def _add(target: dict[str, int], delta: Mapping[str, int]) -> None:
    for key in _COUNTER_KEYS:
        target[key] = _nonnegative_int(target.get(key)) + _nonnegative_int(delta.get(key))


def _estimate_model_cost(model: str, counters: Mapping[str, int]) -> float | None:
    rates = _PRICING_PER_MILLION.get(model)
    if rates is None:
        return None

    input_text = _nonnegative_int(counters.get("input_text_tokens"))
    input_audio = _nonnegative_int(counters.get("input_audio_tokens"))
    input_image = _nonnegative_int(counters.get("input_image_tokens"))
    cached_text = min(input_text, _nonnegative_int(counters.get("cached_text_tokens")))
    cached_audio = min(input_audio, _nonnegative_int(counters.get("cached_audio_tokens")))
    cached_image = min(input_image, _nonnegative_int(counters.get("cached_image_tokens")))

    cached_total = _nonnegative_int(counters.get("cached_input_tokens"))
    cached_unclassified = max(0, cached_total - cached_text - cached_audio - cached_image)
    input_total = _nonnegative_int(counters.get("input_tokens"))
    input_unclassified = max(0, input_total - input_text - input_audio - input_image)
    uncached_unclassified = max(0, input_unclassified - cached_unclassified)

    output_text = _nonnegative_int(counters.get("output_text_tokens"))
    output_audio = _nonnegative_int(counters.get("output_audio_tokens"))
    output_unclassified = max(
        0,
        _nonnegative_int(counters.get("output_tokens")) - output_text - output_audio,
    )

    million_token_cost = (
        (input_text - cached_text) * rates["input_text"]
        + (input_audio - cached_audio) * rates["input_audio"]
        + (input_image - cached_image) * rates["input_image"]
        + cached_text * rates["cached_text"]
        + cached_audio * rates["cached_audio"]
        + cached_image * rates["cached_image"]
        + cached_unclassified * rates["cached_text"]
        + uncached_unclassified * rates["input_text"]
        + (output_text + output_unclassified) * rates["output_text"]
        + output_audio * rates["output_audio"]
    )
    return million_token_cost / 1_000_000.0


def _aggregate(models: Mapping[str, Mapping[str, int]]) -> dict[str, Any]:
    counters = _empty_counters()
    estimated_cost = 0.0
    unpriced_models: list[str] = []
    for model, model_counters in models.items():
        _add(counters, model_counters)
        model_cost = _estimate_model_cost(model, model_counters)
        if model_cost is None:
            unpriced_models.append(model)
        else:
            estimated_cost += model_cost
    return {
        **counters,
        "estimated_cost_usd": (
            None if unpriced_models else round(estimated_cost, 8)
        ),
        "pricing_complete": not unpriced_models,
        "unpriced_models": sorted(unpriced_models),
    }


class UsageTracker:
    """Persist cumulative Realtime response usage without storing conversation data."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._session_started_at = _now()
        self._tracking_started_at = self._session_started_at
        self._lifetime_models: dict[str, dict[str, int]] = {}
        self._session_models: dict[str, dict[str, int]] = {}
        self._load()

    def record(self, model: str, usage: object) -> dict[str, Any] | None:
        delta = normalize_usage(usage)
        if delta["total_tokens"] == 0 and delta["input_tokens"] == 0 and delta["output_tokens"] == 0:
            return None
        normalized_model = model.strip() or "unknown"
        with self._lock:
            lifetime = self._lifetime_models.setdefault(normalized_model, _empty_counters())
            session = self._session_models.setdefault(normalized_model, _empty_counters())
            _add(lifetime, delta)
            _add(session, delta)
            self._persist_locked()
            return {
                "response": {
                    **delta,
                    "estimated_cost_usd": _estimate_model_cost(normalized_model, delta),
                },
                "lifetime": _aggregate(self._lifetime_models),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            models: dict[str, dict[str, Any]] = {}
            for model, counters in self._lifetime_models.items():
                models[model] = {
                    **counters,
                    "estimated_cost_usd": _estimate_model_cost(model, counters),
                    "pricing_known": model in _PRICING_PER_MILLION,
                }
            return {
                "tracking_started_at": self._tracking_started_at,
                "session_started_at": self._session_started_at,
                "currency": "USD",
                "estimate_only": True,
                "pricing_source": PRICING_SOURCE,
                "pricing_as_of": PRICING_AS_OF,
                "lifetime": _aggregate(self._lifetime_models),
                "session": _aggregate(self._session_models),
                "models": models,
            }

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("usage file root is not an object")
            models = payload.get("models", {})
            if not isinstance(models, dict):
                raise TypeError("usage models are not an object")
            loaded: dict[str, dict[str, int]] = {}
            for model, raw_counters in models.items():
                if not isinstance(model, str) or not isinstance(raw_counters, dict):
                    continue
                loaded[model] = {
                    key: _nonnegative_int(raw_counters.get(key)) for key in _COUNTER_KEYS
                }
            self._lifetime_models = loaded
            tracking_started_at = payload.get("tracking_started_at")
            if isinstance(tracking_started_at, str) and tracking_started_at:
                self._tracking_started_at = tracking_started_at
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning("Could not load persisted Realtime usage", exc_info=True)

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)
            temp_path = self._path.with_name(f"{self._path.name}.tmp")
            temp_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "tracking_started_at": self._tracking_started_at,
                        "models": self._lifetime_models,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            temp_path.chmod(0o600)
            temp_path.replace(self._path)
            self._path.chmod(0o600)
        except OSError:
            logger.warning("Could not persist Realtime usage", exc_info=True)
