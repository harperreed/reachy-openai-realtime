import json
from types import SimpleNamespace

import pytest

from reachy_openai_realtime.usage import UsageTracker, normalize_usage


def sample_usage() -> dict[str, object]:
    return {
        "total_tokens": 253,
        "input_tokens": 132,
        "output_tokens": 121,
        "input_token_details": {
            "text_tokens": 119,
            "audio_tokens": 13,
            "image_tokens": 0,
            "cached_tokens": 64,
            "cached_tokens_details": {
                "text_tokens": 64,
                "audio_tokens": 0,
                "image_tokens": 0,
            },
        },
        "output_token_details": {
            "text_tokens": 30,
            "audio_tokens": 91,
        },
    }


def test_normalize_usage_accepts_sdk_style_objects() -> None:
    raw = sample_usage()
    input_details = raw["input_token_details"]
    output_details = raw["output_token_details"]
    assert isinstance(input_details, dict)
    assert isinstance(output_details, dict)
    usage = SimpleNamespace(
        total_tokens=raw["total_tokens"],
        input_tokens=raw["input_tokens"],
        output_tokens=raw["output_tokens"],
        input_token_details=SimpleNamespace(
            text_tokens=input_details["text_tokens"],
            audio_tokens=input_details["audio_tokens"],
            image_tokens=input_details["image_tokens"],
            cached_tokens=input_details["cached_tokens"],
            cached_tokens_details=SimpleNamespace(
                **input_details["cached_tokens_details"]
            ),
        ),
        output_token_details=SimpleNamespace(**output_details),
    )

    normalized = normalize_usage(usage)

    assert normalized["total_tokens"] == 253
    assert normalized["input_audio_tokens"] == 13
    assert normalized["cached_text_tokens"] == 64
    assert normalized["output_audio_tokens"] == 91


def test_usage_tracker_persists_lifetime_totals_and_estimated_cost(tmp_path) -> None:
    path = tmp_path / "private" / "usage.json"
    tracker = UsageTracker(path)

    first = tracker.record("gpt-realtime-2.1", sample_usage())

    assert first is not None
    assert first["response"]["estimated_cost_usd"] == pytest.approx(0.0072056)
    assert path.exists()
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    reloaded = UsageTracker(path)
    lifetime = reloaded.snapshot()["lifetime"]
    session = reloaded.snapshot()["session"]
    assert lifetime["total_tokens"] == 253
    assert lifetime["input_tokens"] == 132
    assert lifetime["output_tokens"] == 121
    assert lifetime["estimated_cost_usd"] == pytest.approx(0.0072056)
    assert session["total_tokens"] == 0

    reloaded.record("gpt-realtime-2.1", sample_usage())
    assert reloaded.snapshot()["lifetime"]["total_tokens"] == 506
    assert reloaded.snapshot()["lifetime"]["estimated_cost_usd"] == pytest.approx(
        0.0144112
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "conversation" not in payload
    assert "transcript" not in payload


def test_unknown_model_tracks_tokens_without_guessing_price() -> None:
    tracker = UsageTracker()

    tracker.record("custom-realtime-model", sample_usage())
    snapshot = tracker.snapshot()

    assert snapshot["lifetime"]["total_tokens"] == 253
    assert snapshot["lifetime"]["estimated_cost_usd"] is None
    assert snapshot["lifetime"]["pricing_complete"] is False
    assert snapshot["lifetime"]["unpriced_models"] == ["custom-realtime-model"]
