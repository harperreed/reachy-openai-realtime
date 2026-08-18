# ABOUTME: Integration test for GET /api/health endpoint (hardening spec §23).
# ABOUTME: Verifies the route returns the correct six-key shape and reflects live health.
from fastapi.testclient import TestClient

from reachy_openai_realtime.main import ReachyOpenaiRealtime


def test_health_route_reports_spec_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path / "config"))
    app = ReachyOpenaiRealtime()
    client = TestClient(app.settings_app)

    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ok", "realtime", "microphone", "speaker", "motion", "camera"}
    assert body["ok"] is False  # no session running in this test

    app.status.set_phase("connected", "ok", connected=True)
    app.status.set_component_health("microphone", True)
    app.status.set_component_health("speaker", True)
    assert client.get("/api/health").json()["ok"] is True
