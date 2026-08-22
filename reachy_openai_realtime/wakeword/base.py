# ABOUTME: Wake-word detector abstraction — the value type and Protocol every
# ABOUTME: backend implements. No presence or OpenAI logic here (spec §6).
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WakeWordDetection:
    phrase: str
    score: float
    detected_at: float


class WakeWordDetector(Protocol):
    @property
    def required_sample_rate(self) -> int: ...

    def start(self) -> None: ...

    def process(self, pcm16: bytes) -> WakeWordDetection | None: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...
