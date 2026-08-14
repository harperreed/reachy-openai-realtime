from fastapi.testclient import TestClient

from reachy_openai_realtime.main import ReachyOpenaiRealtime
from reachy_openai_realtime.settings import env_path


def test_settings_api_accepts_json_body_and_never_returns_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REACHY_OPENAI_REALTIME_LANGUAGE", raising=False)
    app = ReachyOpenaiRealtime()
    assert app.settings_app is not None
    client = TestClient(app.settings_app)
    key = "sk-test-abcdefghijklmnopqrstuvwxyz"

    response = client.post("/api/config/api-key", json={"api_key": key})
    assert response.status_code == 200
    assert response.json() == {"configured": True, "restart_required": False}
    assert key not in response.text
    assert env_path().read_text(encoding="utf-8") == f"OPENAI_API_KEY={key}\n"

    config_response = client.get("/api/config")
    assert config_response.status_code == 200
    assert config_response.json()["configured"] is True
    assert config_response.json()["app_name"] == "Reachy Mini OpenAI Realtime"
    assert config_response.json()["language"] == "en"
    assert config_response.json()["languages"][0] == {"code": "en", "label": "English"}
    assert config_response.json()["camera_available"] is False
    assert config_response.json()["camera_enabled"] is False
    assert config_response.json()["camera_sent_to_openai"] is False
    assert config_response.json()["camera_send_mode"] == "speech_start_snapshot"
    assert key not in config_response.text

    camera_response = client.post("/api/config/camera", json={"enabled": True})
    assert camera_response.status_code == 409

    snapshot_response = client.get("/api/camera/snapshot")
    assert snapshot_response.status_code == 403

    status_response = client.get("/api/status")
    assert status_response.status_code == 200
    assert status_response.json()["phase"] == "starting"
    assert "events" in status_response.json()
    assert status_response.json()["mic_dbfs"] is None
    assert status_response.json()["camera_images_sent"] == 0
    assert key not in status_response.text

    language_response = client.post("/api/config/language", json={"language": "ja"})
    assert language_response.status_code == 200
    assert language_response.json() == {
        "language": "ja",
        "label": "日本語",
        "restart_required": False,
    }
    assert key not in language_response.text
    assert "REACHY_OPENAI_REALTIME_LANGUAGE=ja" in env_path().read_text(encoding="utf-8")

    invalid_language = client.post("/api/config/language", json={"language": "xx"})
    assert invalid_language.status_code == 400
    assert invalid_language.json() == {"detail": "対応していない言語です"}


def test_settings_api_rejects_short_key_without_echoing_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REACHY_OPENAI_REALTIME_LANGUAGE", raising=False)
    app = ReachyOpenaiRealtime()
    assert app.settings_app is not None
    client = TestClient(app.settings_app)

    response = client.post("/api/config/api-key", json={"api_key": "short"})
    assert response.status_code == 400
    assert response.json() == {"detail": "APIキーが短すぎます"}
    assert "short" not in response.text
