# ABOUTME: Tests for memory configuration fields (spec §12) and the
# ABOUTME: memory.sqlite path helper in settings.
from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.settings import memory_db_path


def test_memory_defaults_match_spec():
    config = AppConfig()
    assert config.memory_enabled is True
    assert config.memory_write_policy == "agent"
    assert config.memory_wake_char_budget == 2000
    assert config.memory_nap_model == "gpt-5-mini"
    assert config.memory_nap_min_interval_s == 900
    assert config.memory_nap_chunk_size == 20
    assert config.memory_nap_branching == 8
    assert config.memory_nap_max_nodes == 10


def test_memory_env_overrides(monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_MEMORY", "off")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_MEMORY_WRITE_POLICY", "explicit")
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_NAP_MODEL", "gpt-5-mini-2026-01-01")
    config = AppConfig.from_env()
    assert config.memory_enabled is False
    assert config.memory_write_policy == "explicit"
    assert config.memory_nap_model == "gpt-5-mini-2026-01-01"


def test_memory_write_policy_rejects_junk(monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_MEMORY_WRITE_POLICY", "yolo")
    config = AppConfig.from_env()
    assert config.memory_write_policy == "agent"


def test_memory_db_path_lives_in_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path))
    assert memory_db_path() == tmp_path / "memory.sqlite"
