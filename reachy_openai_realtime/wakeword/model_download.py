# ABOUTME: Downloads and verifies the pinned Edge Impulse wake-word .eim model.
# ABOUTME: stdlib-only (urllib); sha256-pinned so a corrupt or swapped file is rejected.
from __future__ import annotations

import hashlib
import logging
import os
import stat
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..settings import models_dir

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 1 << 20  # 1 MiB streamed reads keep the whole model out of memory
_DOWNLOAD_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class WakeModelPin:
    """A specific, verifiable build of the wake-word model."""

    filename: str
    url: str
    size_bytes: int
    sha256: str


# The hey_reachy wake-word model, pinned to one immutable Hugging Face revision.
# The Space carries no redistribution license, so we download at runtime and never
# vendor the binary (spec addendum A2). The revision, size, and sha256 below were
# captured from that exact commit; a mismatch means the file is not what we pinned.
HEY_REACHY_AARCH64 = WakeModelPin(
    filename="hey-reachy-wake-word-detection-linux-aarch64.eim",
    url=(
        "https://huggingface.co/spaces/"
        "luisomoreau/hey_reachy_wake_word_detection/resolve/"
        "3b6670748dc3ffda9f09dce18810b283ace7147e/"
        "hey_reachy_wake_word_detection/models/"
        "hey-reachy-wake-word-detection-linux-aarch64.eim"
    ),
    size_bytes=13_574_768,
    sha256="9861b8d43bd9a2b95bf0105262d358c9f6b5aa17fa0b266b0dadae8328c3f229",
)


class WakeModelError(RuntimeError):
    """The wake model could not be provisioned (download or verification failed)."""


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matches(path: Path, pin: WakeModelPin) -> bool:
    try:
        if path.stat().st_size != pin.size_bytes:
            return False
    except OSError:
        return False
    return _sha256_of(path) == pin.sha256


def _describe(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "missing file"
    return f"sha256 {_sha256_of(path)} size {size}"


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _download_to(url: str, dest: Path, *, timeout: float = _DOWNLOAD_TIMEOUT_SECONDS) -> None:
    """Stream url into dest (truncating), one chunk at a time. stdlib only."""

    request = urllib.request.Request(url, headers={"User-Agent": "reachy-openai-realtime"})
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as handle:
        while True:
            chunk = response.read(_CHUNK_SIZE)
            if not chunk:
                break
            handle.write(chunk)


def ensure_wake_model(
    pin: WakeModelPin = HEY_REACHY_AARCH64,
    *,
    dest_dir: Path | None = None,
    download: Callable[[str, Path], None] = _download_to,
) -> Path:
    """Return a verified, executable path to the pinned .eim, downloading if needed.

    Idempotent: an on-disk file whose size and sha256 already match the pin is
    reused untouched. Any failure — network, disk, or a size/sha256 mismatch —
    raises WakeModelError and leaves no partial file behind.
    """

    directory = dest_dir if dest_dir is not None else models_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / pin.filename

    if _matches(target, pin):
        _make_executable(target)
        logger.info("wake model already present: %s", target)
        return target

    partial = target.with_suffix(target.suffix + ".partial")
    try:
        logger.info("downloading wake model from %s", pin.url)
        try:
            download(pin.url, partial)
        except OSError as error:  # URLError, socket timeout, and disk errors all subclass OSError
            raise WakeModelError(f"wake model download failed: {error}") from error
        if not _matches(partial, pin):
            raise WakeModelError(
                f"wake model verification failed: expected sha256 {pin.sha256} "
                f"size {pin.size_bytes}, got {_describe(partial)}"
            )
        _make_executable(partial)
        os.replace(partial, target)  # atomic swap into place; readers never see a half file
    finally:
        partial.unlink(missing_ok=True)

    logger.info("wake model ready: %s", target)
    return target
