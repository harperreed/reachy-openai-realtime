# ABOUTME: Startup logging tests for main.attach_file_logging.
# ABOUTME: Covers the fresh-install path where the config dir does not exist yet.
import logging

from reachy_openai_realtime.main import attach_file_logging
from reachy_openai_realtime.observability.events import redact_secrets
from reachy_openai_realtime.settings import log_path


def test_attach_file_logging_creates_config_dir_on_fresh_install(monkeypatch, tmp_path) -> None:
    """A fresh install has no config dir; attaching file logging must create it, not crash.

    Regression: on a factory Reachy Mini the app died at startup with
    FileNotFoundError because RotatingFileHandler opened application.log
    before anything had created ~/.config/reachy-mini/apps/reachy_openai_realtime/.
    """
    fresh = tmp_path / "apps" / "reachy_openai_realtime"
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(fresh))
    assert not fresh.exists()

    handler = attach_file_logging()
    try:
        assert oct(fresh.stat().st_mode & 0o777) == "0o700"
        logging.getLogger("fresh-install-test").warning("fresh install smoke line")
        handler.flush()
        assert "fresh install smoke line" in log_path().read_text(encoding="utf-8")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_application_log_redacts_keys_including_exception_text(tmp_path, monkeypatch) -> None:
    """OpenAI-style keys must not reach disk — neither via %s-formatted messages
    nor via exception tracebacks (which a logging.Filter cannot see)."""
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path))
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz"
    handler = attach_file_logging()
    logger = logging.getLogger("redaction-test")
    try:
        logger.warning("connecting with %s", secret)
        try:
            raise RuntimeError(f"auth failed for {secret}")
        except RuntimeError:
            logger.exception("boom")
    finally:
        handler.close()
        logging.getLogger().removeHandler(handler)

    written = log_path().read_text(encoding="utf-8")
    assert secret not in written                      # neither the %s-formatted message...
    assert redact_secrets(secret) in written          # ...nor the traceback text leaks
