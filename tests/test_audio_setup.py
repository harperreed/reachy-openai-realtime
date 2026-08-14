from typing import Any

from reachy_openai_realtime.audio_setup import (
    WIRELESS_CONVERSATION_AUDIO_CONFIG,
    apply_wireless_conversation_audio_config,
)


class FakeAudio:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, bool, float]] = []

    def apply_audio_config(
        self,
        config: Any,
        *,
        verify: bool,
        write_settle_seconds: float,
    ) -> bool:
        self.calls.append((config, verify, write_settle_seconds))
        return True


class FakeRobot:
    def __init__(self, audio: object | None) -> None:
        self.media = type("Media", (), {"audio": audio})()


def test_wireless_audio_config_uses_sdk_api() -> None:
    audio = FakeAudio()
    assert apply_wireless_conversation_audio_config(FakeRobot(audio)) is True
    assert audio.calls == [(WIRELESS_CONVERSATION_AUDIO_CONFIG, True, 0.1)]


def test_wireless_audio_config_is_optional() -> None:
    assert apply_wireless_conversation_audio_config(FakeRobot(None)) is False
