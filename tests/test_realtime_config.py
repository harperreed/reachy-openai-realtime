from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.realtime import RealtimeRobotSession


def test_session_uses_client_turn_detection_and_far_field_noise_reduction() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()

    config = session._session_config()
    audio_input = config["audio"]["input"]

    assert audio_input["turn_detection"] is None
    assert audio_input["noise_reduction"] == {"type": "far_field"}
    assert "transcription" not in audio_input
    assert "configured conversation language is English" in config["instructions"]


def test_session_language_provider_changes_response_language() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session._language_provider = lambda: "ja"

    assert session._current_language() == "ja"
    assert "Japanese" in session._session_config()["instructions"]
