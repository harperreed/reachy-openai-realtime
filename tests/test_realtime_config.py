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


def test_recorded_moves_instructions_tell_model_to_offer_examples() -> None:
    """Field report: 'reachy doesn't know what emotes or dances are available' —
    a bare name dump is never volunteered; the model needs telling to offer some."""
    text = recorded_moves_instructions(["happy1"], ["spin"])
    assert "examples" in text
    assert "never read out every name" in text


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


def test_wake_defaults_match_spec():
    config = AppConfig()
    assert config.wake_enabled is True
    assert config.wake_backend == "edge_impulse"
    assert config.wake_phrase == "hey reachy"
    assert config.wake_model_path == "models/hey-reachy-wake-word-detection-linux-aarch64.eim"
    assert config.wake_threshold == 0.70
    assert config.wake_debounce_seconds == 2.0
    assert config.wake_history_seconds == 4.0
    assert config.wake_preroll_ms == 400
    assert config.max_wake_buffer_seconds == 10
    assert config.wake_motion_enabled is True
    assert config.boot_motion_enabled is True


def test_wake_settings_are_clamped_to_spec_ranges():
    config = AppConfig(
        wake_threshold=5.0,
        wake_debounce_seconds=0.1,
        wake_history_seconds=99.0,
        wake_preroll_ms=5,
        max_wake_buffer_seconds=100,
    )
    assert config.wake_threshold == 1.0          # 0.0 < t <= 1.0
    assert config.wake_debounce_seconds == 0.5   # [0.5, 10]
    assert config.wake_history_seconds == 10.0   # [1, 10]
    assert config.wake_preroll_ms == 100         # [100, 1000]
    assert config.max_wake_buffer_seconds == 30  # [2, 30]


def test_wake_threshold_zero_or_negative_falls_back_to_default():
    assert AppConfig(wake_threshold=0.0).wake_threshold == 0.70
    assert AppConfig(wake_threshold=-1.0).wake_threshold == 0.70


def test_from_env_parses_wake_settings(monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_WAKE_ENABLED", "0")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_WAKE_THRESHOLD", "0.9")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_WAKE_PREROLL_MS", "250")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_WAKE_PHRASE", "hey robot")
    config = AppConfig.from_env()
    assert config.wake_enabled is False
    assert config.wake_threshold == 0.9
    assert config.wake_preroll_ms == 250
    assert config.wake_phrase == "hey robot"


def test_from_env_wake_bad_numbers_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_WAKE_THRESHOLD", "not-a-number")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_WAKE_PREROLL_MS", "garbage")
    config = AppConfig.from_env()
    assert config.wake_threshold == 0.70
    assert config.wake_preroll_ms == 400


def test_from_env_wake_enabled_defaults_true_when_unset(monkeypatch):
    monkeypatch.delenv("REACHY_OPENAI_REALTIME_WAKE_ENABLED", raising=False)
    assert AppConfig.from_env().wake_enabled is True
