import os

import pytest

from reachy_openai_realtime.settings import (
    env_path,
    load_instance_env,
    remove_api_key,
    save_api_key,
    save_language,
    usage_path,
)


def configure_test_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REACHY_OPENAI_REALTIME_LANGUAGE", raising=False)


def test_api_key_is_persisted_outside_package(tmp_path, monkeypatch) -> None:
    configure_test_dir(tmp_path, monkeypatch)
    key = "sk-test-abcdefghijklmnopqrstuvwxyz"
    target = save_api_key(key)
    assert target == tmp_path / "config" / ".env"
    assert target.read_text(encoding="utf-8") == f"OPENAI_API_KEY={key}\n"
    assert oct(target.parent.stat().st_mode & 0o777) == "0o700"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert os.environ["OPENAI_API_KEY"] == key
    assert usage_path() == tmp_path / "config" / "usage.json"


def test_instance_env_can_be_loaded_and_removed(tmp_path, monkeypatch) -> None:
    configure_test_dir(tmp_path, monkeypatch)
    key = "sk-test-abcdefghijklmnopqrstuvwxyz"
    target = env_path()
    target.parent.mkdir(parents=True)
    target.write_text(f"OPENAI_API_KEY={key}\n", encoding="utf-8")
    load_instance_env()
    assert os.environ["OPENAI_API_KEY"] == key
    assert remove_api_key() is True
    assert "OPENAI_API_KEY" not in os.environ


def test_legacy_package_env_is_still_loaded(tmp_path, monkeypatch) -> None:
    configure_test_dir(tmp_path, monkeypatch)
    legacy_dir = tmp_path / "site-packages" / "reachy_japanese_realtime"
    legacy_dir.mkdir(parents=True)
    key = "sk-test-legacy-abcdefghijklmnop"
    (legacy_dir / ".env").write_text(f"OPENAI_API_KEY={key}\n", encoding="utf-8")
    assert load_instance_env(legacy_dir) == legacy_dir / ".env"
    assert os.environ["OPENAI_API_KEY"] == key


def test_newline_injection_is_rejected(tmp_path, monkeypatch) -> None:
    configure_test_dir(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="使用できない文字"):
        save_api_key("sk-test-abcdefghijklmnop\nOTHER_SECRET=bad")


def test_language_is_validated_and_preserves_api_key(tmp_path, monkeypatch) -> None:
    configure_test_dir(tmp_path, monkeypatch)
    key = "sk-test-abcdefghijklmnopqrstuvwxyz"
    save_api_key(key)

    save_language("ja")

    assert env_path().read_text(encoding="utf-8") == (
        f"OPENAI_API_KEY={key}\nREACHY_OPENAI_REALTIME_LANGUAGE=ja\n"
    )
    with pytest.raises(ValueError, match="Unsupported language"):
        save_language("xx")


def test_events_and_log_paths_live_in_config_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path))
    from reachy_openai_realtime import settings

    assert settings.events_path() == tmp_path / "events.jsonl"
    assert settings.log_path() == tmp_path / "application.log"


def test_legacy_persistent_config_is_migrated(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", raising=False)
    monkeypatch.delenv("REACHY_JAPANESE_REALTIME_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    legacy = tmp_path / "xdg" / "reachy-mini" / "apps" / "reachy_japanese_realtime" / ".env"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("OPENAI_API_KEY=sk-test-legacy-abcdefghijklmnop\n", encoding="utf-8")

    loaded = load_instance_env()

    assert loaded == env_path()
    assert loaded.read_text(encoding="utf-8") == legacy.read_text(encoding="utf-8")
    assert oct(loaded.stat().st_mode & 0o777) == "0o600"
