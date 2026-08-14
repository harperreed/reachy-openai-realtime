from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tuned XVF3800 parameters used by Pollen Robotics' official conversation app.
WIRELESS_CONVERSATION_AUDIO_CONFIG = (
    ("PP_AGCMAXGAIN", (10.0,)),
    ("PP_MIN_NS", (0.8,)),
    ("PP_MIN_NN", (0.8,)),
    ("PP_GAMMA_E", (0.5,)),
    ("PP_GAMMA_ETAIL", (0.5,)),
    ("PP_NLATTENONOFF", (0,)),
    ("PP_MGSCALE", (4.0, 1.0, 1.0)),
)


def apply_wireless_conversation_audio_config(robot: Any) -> bool:
    """Apply conversation-oriented ReSpeaker settings when the SDK supports it."""
    audio = getattr(getattr(robot, "media", None), "audio", None)
    apply_audio_config = getattr(audio, "apply_audio_config", None)
    if not callable(apply_audio_config):
        logger.info("Reachy audio tuning API unavailable; using the current device settings")
        return False
    try:
        return bool(
            apply_audio_config(
                WIRELESS_CONVERSATION_AUDIO_CONFIG,
                verify=True,
                write_settle_seconds=0.1,
            )
        )
    except Exception:
        logger.warning("Could not apply Reachy conversation audio settings", exc_info=True)
        return False
