# ABOUTME: Tests for the pinned, sha256-verified wake-model download helper.
# ABOUTME: Uses a tiny fake pin and an injected writer so no test touches the network.
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reachy_openai_realtime.settings import config_dir, models_dir
from reachy_openai_realtime.wakeword.model_download import (
    WakeModelError,
    WakeModelPin,
    ensure_wake_model,
)

PAYLOAD = b"fake eim model bytes " * 16
PIN = WakeModelPin(
    filename="test-model.eim",
    url="https://example.invalid/test-model.eim",
    size_bytes=len(PAYLOAD),
    sha256=hashlib.sha256(PAYLOAD).hexdigest(),
)


def _writer(payload: bytes):
    """Build a download() double that records its calls and writes fixed bytes."""

    calls: list[str] = []

    def download(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(payload)

    download.calls = calls  # type: ignore[attr-defined]
    return download


def test_models_dir_is_under_config_dir():
    assert models_dir() == config_dir() / "models"


def test_downloads_and_verifies_then_marks_executable(tmp_path):
    download = _writer(PAYLOAD)
    result = ensure_wake_model(PIN, dest_dir=tmp_path, download=download)
    assert result == tmp_path / "test-model.eim"
    assert result.read_bytes() == PAYLOAD
    assert result.stat().st_mode & 0o111  # the runner must be executable
    assert download.calls == [PIN.url]


def test_existing_valid_model_is_not_redownloaded(tmp_path):
    download = _writer(PAYLOAD)
    ensure_wake_model(PIN, dest_dir=tmp_path, download=download)
    ensure_wake_model(PIN, dest_dir=tmp_path, download=download)
    assert download.calls == [PIN.url]  # second call reused the verified file


def test_sha256_mismatch_raises_and_leaves_no_partial(tmp_path):
    # Same byte length as the pin, different content ⇒ only sha256 can catch it.
    download = _writer(b"X" * len(PAYLOAD))
    with pytest.raises(WakeModelError):
        ensure_wake_model(PIN, dest_dir=tmp_path, download=download)
    assert list(tmp_path.iterdir()) == []  # no target, no .partial left behind


def test_corrupt_existing_model_is_replaced(tmp_path):
    target = tmp_path / PIN.filename
    target.write_bytes(b"stale wrong contents")
    download = _writer(PAYLOAD)
    result = ensure_wake_model(PIN, dest_dir=tmp_path, download=download)
    assert result.read_bytes() == PAYLOAD
    assert download.calls == [PIN.url]  # the corrupt file forced a re-download


def test_download_failure_is_wrapped(tmp_path):
    def boom(url: str, dest: Path) -> None:
        raise OSError("connection reset")

    with pytest.raises(WakeModelError):
        ensure_wake_model(PIN, dest_dir=tmp_path, download=boom)
    assert list(tmp_path.iterdir()) == []
