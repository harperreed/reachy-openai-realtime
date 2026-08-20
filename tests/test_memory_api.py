# ABOUTME: Route tests for GET /api/memory, POST /api/memory/{id}/pin, DELETE /api/memory/{id}.
# ABOUTME: Follows the TestClient + ReachyOpenaiRealtime pattern from test_settings_api.py.
from fastapi.testclient import TestClient

from reachy_openai_realtime.main import ReachyOpenaiRealtime
from reachy_openai_realtime.memory.manager import MemoryManager
from reachy_openai_realtime.memory.store import MemoryStore


def make_app_with_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.open()
    manager = MemoryManager(store)
    manager._healthy = True  # opened synchronously above
    app = ReachyOpenaiRealtime()
    app.memory_manager = manager
    return app, store, TestClient(app.settings_app)


def test_get_memory_returns_empty_when_manager_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path / "config"))
    app = ReachyOpenaiRealtime()
    client = TestClient(app.settings_app)
    resp = client.get("/api/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "memory unavailable"
    assert body["memories"] == []
    assert body["count"] == 0


def test_get_memory_lists_notes_newest_first(tmp_path, monkeypatch):
    app, _store, client = make_app_with_memory(tmp_path, monkeypatch)
    import asyncio
    first_id = asyncio.run(app.memory_manager.note("first note"))
    second_id = asyncio.run(app.memory_manager.note("second note"))
    resp = client.get("/api/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 2
    ids = [m["id"] for m in body["memories"]]
    assert ids == [second_id.id, first_id.id]
    first_mem = body["memories"][1]
    assert set(first_mem.keys()) == {"id", "kind", "text", "pinned", "source", "created_at"}


def test_get_memory_filters_by_query(tmp_path, monkeypatch):
    app, _store, client = make_app_with_memory(tmp_path, monkeypatch)
    import asyncio
    asyncio.run(app.memory_manager.note("ukulele lessons on tuesday"))
    asyncio.run(app.memory_manager.note("accordion practice on friday"))
    resp = client.get("/api/memory", params={"q": "ukulele"})
    body = resp.json()
    assert body["ok"] is True
    assert len(body["memories"]) == 1
    assert "ukulele" in body["memories"][0]["text"]
    assert body["count"] == 2  # total, not filtered count


def test_pin_memory_toggles_pin_and_rejects_unknown(tmp_path, monkeypatch):
    app, store, client = make_app_with_memory(tmp_path, monkeypatch)
    import asyncio
    note = asyncio.run(app.memory_manager.note("pin this"))
    resp = client.post(f"/api/memory/{note.id}/pin", json={"pinned": True})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert store.get_note(note.id).pinned is True

    resp = client.post("/api/memory/mem_missing/pin", json={"pinned": True})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "error": "unknown memory id"}


def test_delete_memory_removes_note_and_rejects_unknown(tmp_path, monkeypatch):
    app, store, client = make_app_with_memory(tmp_path, monkeypatch)
    import asyncio
    note = asyncio.run(app.memory_manager.note("delete me"))
    resp = client.delete(f"/api/memory/{note.id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert store.get_note(note.id) is None

    resp = client.delete("/api/memory/mem_missing")
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "error": "unknown memory id"}


def test_pin_and_delete_return_unavailable_when_manager_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path / "config"))
    app = ReachyOpenaiRealtime()
    client = TestClient(app.settings_app)
    resp = client.post("/api/memory/mem_x/pin", json={"pinned": True})
    assert resp.json() == {"ok": False, "error": "memory unavailable"}
    resp = client.delete("/api/memory/mem_x")
    assert resp.json() == {"ok": False, "error": "memory unavailable"}
