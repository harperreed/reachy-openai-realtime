from reachy_openai_realtime.config import AppConfig, recorded_moves_instructions
from reachy_openai_realtime.motion import TOOL_DEFINITIONS
from reachy_openai_realtime.realtime import RealtimeRobotSession


def test_recorded_moves_instructions_empty_when_no_catalogs() -> None:
    assert recorded_moves_instructions([], []) == ""


def test_recorded_moves_instructions_lists_names() -> None:
    text = recorded_moves_instructions(["happy1", "sad2"], ["spin"])
    assert text.startswith("\n\n")
    assert "happy1, sad2" in text and "spin" in text
    assert "play_emotion" in text and "play_dance" in text


def test_recorded_moves_instructions_omits_absent_catalog() -> None:
    text = recorded_moves_instructions(["happy1"], [])
    assert "play_emotion" in text and "happy1" in text
    assert "play_dance" not in text


def test_recorded_moves_instructions_prefer_recorded_over_express() -> None:
    """Without an explicit preference the model picks express (its enum matches
    'show happy' requests directly) and the recorded library never plays."""
    text = recorded_moves_instructions(["happy1"], ["spin"])
    assert "prefer play_emotion / play_dance" in text
    assert "express" in text

    emotions_only = recorded_moves_instructions(["happy1"], [])
    assert "prefer play_emotion" in emotions_only
    assert "play_dance" not in emotions_only


class _StubMotion:
    """Minimal MotionManager face for _session_config: bare tools, no catalogs."""

    def tool_definitions(self):
        return list(TOOL_DEFINITIONS)

    def emotion_names(self):
        return []

    def dance_names(self):
        return []


def test_session_config_advertises_catalog_tools_and_names() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()

    class _CatalogMotion(_StubMotion):
        def tool_definitions(self):
            return list(TOOL_DEFINITIONS) + [{"type": "function", "name": "play_emotion"}]

        def emotion_names(self):
            return ["happy1", "sad2"]

    session.motion = _CatalogMotion()
    config = session._session_config()
    assert any(tool.get("name") == "play_emotion" for tool in config["tools"])
    assert "happy1, sad2" in config["instructions"]


def test_session_config_without_catalogs_keeps_base_tools_only() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session.motion = _StubMotion()
    config = session._session_config()
    assert [tool["name"] for tool in config["tools"]] == [tool["name"] for tool in TOOL_DEFINITIONS]
    assert "play_emotion accepts" not in config["instructions"]


def test_session_uses_client_turn_detection_and_far_field_noise_reduction() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session.motion = _StubMotion()

    config = session._session_config()
    audio_input = config["audio"]["input"]

    assert audio_input["turn_detection"] is None
    assert audio_input["noise_reduction"] == {"type": "far_field"}
    assert "transcription" not in audio_input
    assert "configured conversation language is English" in config["instructions"]


def test_session_language_provider_changes_response_language() -> None:
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session.motion = _StubMotion()
    session._language_provider = lambda: "ja"

    assert session._current_language() == "ja"
    assert "Japanese" in session._session_config()["instructions"]
