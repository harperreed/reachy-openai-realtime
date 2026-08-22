from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VadDecision:
    started: bool = False
    stopped: bool = False
    reason: str | None = None


class EnergyTurnDetector:
    """Small adaptive energy VAD for Reachy Mini's processed microphone feed."""

    def __init__(
        self,
        *,
        start_margin_db: float = 5.0,
        continue_margin_db: float = 2.0,
        min_speech_ms: float = 240.0,
        end_silence_ms: float = 800.0,
        max_turn_ms: float = 20_000.0,
    ) -> None:
        self.start_margin_db = start_margin_db
        self.continue_margin_db = continue_margin_db
        self.min_speech_ms = min_speech_ms
        self.end_silence_ms = end_silence_ms
        self.max_turn_ms = max_turn_ms
        self.noise_floor_dbfs = -50.0
        self.speech_active = False
        self._candidate_ms = 0.0
        self._silence_ms = 0.0
        self._turn_ms = 0.0

    @property
    def start_threshold_dbfs(self) -> float:
        return min(-38.0, max(-56.0, self.noise_floor_dbfs + self.start_margin_db))

    @property
    def continue_threshold_dbfs(self) -> float:
        return min(-40.0, max(-62.0, self.noise_floor_dbfs + self.continue_margin_db))

    def reset_turn(self) -> None:
        self.speech_active = False
        self._candidate_ms = 0.0
        self._silence_ms = 0.0
        self._turn_ms = 0.0

    def begin_turn(self) -> None:
        """Force the detector into an active turn without waiting for the
        energy gate — used when a wake word has already opened the turn and
        the captured pre-roll is being injected. The turn then ends on the
        normal silence / max-duration rules in ``process``."""
        self.speech_active = True
        self._candidate_ms = self.min_speech_ms
        self._silence_ms = 0.0
        self._turn_ms = self.min_speech_ms

    def process(
        self,
        level_dbfs: float,
        duration_ms: float,
        *,
        speech_detected: bool | None = None,
    ) -> VadDecision:
        duration_ms = max(0.0, duration_ms)
        if not self.speech_active:
            voice_gate_open = speech_detected is not False
            if voice_gate_open and level_dbfs >= self.start_threshold_dbfs:
                self._candidate_ms += duration_ms
            else:
                self._candidate_ms = 0.0
                # Explicitly non-speech audio is the best noise-floor sample.
                # Do not learn from audio that the ReSpeaker classified as a
                # human voice, otherwise quiet voices raise their own gate.
                if speech_detected is not True:
                    self._update_noise_floor(level_dbfs, duration_ms)

            if self._candidate_ms >= self.min_speech_ms:
                self.speech_active = True
                self._turn_ms = self._candidate_ms
                self._silence_ms = 0.0
                return VadDecision(started=True, reason="voice")
            return VadDecision()

        self._turn_ms += duration_ms
        # The Wireless microphone can keep reporting a high energy level after
        # the user stops (room noise, fan noise, or residual speaker audio).
        # Once the ReSpeaker explicitly says the sound is not human speech,
        # treat it as silence even when that residual energy remains high.
        if speech_detected is not False and level_dbfs >= self.continue_threshold_dbfs:
            self._silence_ms = 0.0
        else:
            self._silence_ms += duration_ms

        if self._silence_ms >= self.end_silence_ms:
            self.reset_turn()
            return VadDecision(stopped=True, reason="silence")
        if self._turn_ms >= self.max_turn_ms:
            self.reset_turn()
            return VadDecision(stopped=True, reason="maximum")
        return VadDecision()

    def _update_noise_floor(self, level_dbfs: float, duration_ms: float) -> None:
        level_dbfs = max(-70.0, min(-25.0, level_dbfs))
        alpha = min(0.2, duration_ms / 2_000.0)
        self.noise_floor_dbfs = (1.0 - alpha) * self.noise_floor_dbfs + alpha * level_dbfs
