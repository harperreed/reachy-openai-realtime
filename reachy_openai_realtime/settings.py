from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

from .config import LANGUAGE_ENV, language_option

APP_NAME = "reachy_openai_realtime"
LEGACY_APP_NAME = "reachy_japanese_realtime"
CONFIG_DIR_ENV = "REACHY_OPENAI_REALTIME_CONFIG_DIR"
LEGACY_CONFIG_DIR_ENV = "REACHY_JAPANESE_REALTIME_CONFIG_DIR"


def _config_base() -> Path:
    xdg_config_home = os.getenv("XDG_CONFIG_HOME", "").strip()
    return Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config"


def config_dir() -> Path:
    """Return a persistent, user-writable directory outside site-packages."""
    override = os.getenv(CONFIG_DIR_ENV, "").strip()
    if not override:
        override = os.getenv(LEGACY_CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _config_base() / "reachy-mini" / "apps" / APP_NAME


def legacy_config_dir() -> Path:
    return _config_base() / "reachy-mini" / "apps" / LEGACY_APP_NAME


def env_path() -> Path:
    return config_dir() / ".env"


def usage_path() -> Path:
    return config_dir() / "usage.json"


def events_path() -> Path:
    return config_dir() / "events.jsonl"


def log_path() -> Path:
    return config_dir() / "application.log"


def prepare_config_dir() -> Path:
    target_dir = config_dir()
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_dir.chmod(0o700)
    return target_dir


def _migrate_legacy_config() -> Path | None:
    target = env_path()
    legacy = legacy_config_dir() / ".env"
    if target.exists() or not legacy.exists() or target == legacy:
        return target if target.exists() else None
    target_dir = prepare_config_dir()
    temp_path = target_dir / ".env.tmp"
    shutil.copyfile(legacy, temp_path)
    temp_path.chmod(0o600)
    temp_path.replace(target)
    target.chmod(0o600)
    return target


def load_instance_env(legacy_instance_dir: Path | None = None) -> Path | None:
    """Load persistent config, migrating the previous Japanese app config."""
    persistent_path = _migrate_legacy_config() or env_path()
    if persistent_path.exists():
        load_dotenv(dotenv_path=persistent_path, override=True)
        return persistent_path

    if legacy_instance_dir is not None:
        legacy_path = legacy_instance_dir / ".env"
        if legacy_path.exists():
            load_dotenv(dotenv_path=legacy_path, override=True)
            return legacy_path
    return None


def _validated_api_key(api_key: str) -> str:
    value = api_key.strip()
    if len(value) < 20:
        raise ValueError("APIキーが短すぎます")
    if any(character in value for character in ("\n", "\r", "\0")):
        raise ValueError("APIキーに使用できない文字が含まれています")
    return value


def _save_env_value(name: str, value: str) -> Path:
    target_dir = prepare_config_dir()
    target = env_path()
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    updated: list[str] = []
    replaced = False
    for line in existing:
        if line.strip().startswith(f"{name}="):
            updated.append(f"{name}={value}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"{name}={value}")

    temp_path = target_dir / ".env.tmp"
    temp_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temp_path.chmod(0o600)
    temp_path.replace(target)
    target.chmod(0o600)
    os.environ[name] = value
    return target


def save_api_key(api_key: str) -> Path:
    return _save_env_value("OPENAI_API_KEY", _validated_api_key(api_key))


def save_language(language: str) -> Path:
    return _save_env_value(LANGUAGE_ENV, language_option(language).code)


def remove_api_key(legacy_instance_dir: Path | None = None) -> bool:
    removed = False
    paths = [env_path(), legacy_config_dir() / ".env"]
    if legacy_instance_dir is not None:
        paths.append(legacy_instance_dir / ".env")

    for target in dict.fromkeys(paths):
        if not target.exists():
            continue
        lines = [
            line
            for line in target.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("OPENAI_API_KEY=")
        ]
        target.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        target.chmod(0o600)
        removed = True

    os.environ.pop("OPENAI_API_KEY", None)
    return removed
