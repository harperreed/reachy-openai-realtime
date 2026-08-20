# ABOUTME: Tests for MemoryStore: schema migration, append-only notes,
# ABOUTME: persistence across reopen, and normalized-duplicate lookup.
import sqlite3

import pytest

from reachy_openai_realtime.memory.store import MemoryStore, normalize_text


def make_store(tmp_path, **kwargs):
    store = MemoryStore(tmp_path / "memory.sqlite", **kwargs)
    store.open()
    return store


def test_open_creates_schema_and_migration_row(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    store = MemoryStore(db_path)
    store.open()
    try:
        note = store.insert_note("Harper likes espresso", "preference", "agent")
        assert note.id.startswith("mem_")
        assert note.kind == "preference"
        assert note.pinned is False
    finally:
        store.close()
    # Verify the migration row was written atomically alongside the DDL (regression for
    # the executescript-then-INSERT split that left tables present but no version row).
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT version FROM schema_migrations").fetchone()
        assert row is not None and row[0] == 1
    finally:
        conn.close()


def test_note_survives_reopen(tmp_path):
    store = make_store(tmp_path)
    note = store.insert_note("The workshop is in the garage", "place", "agent")
    store.close()
    reopened = make_store(tmp_path)
    try:
        loaded = reopened.get_note(note.id)
        assert loaded is not None
        assert loaded.text == "The workshop is in the garage"
        assert loaded.created_at == note.created_at
    finally:
        reopened.close()


def test_migration_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.close()
    again = make_store(tmp_path)  # second open must not re-run migration 1
    again.close()


def test_normalize_text_casefolds_and_collapses_whitespace():
    assert normalize_text("  Harper   LIKES\tespresso ") == "harper likes espresso"


def test_find_live_note_by_normalized(tmp_path):
    store = make_store(tmp_path)
    try:
        note = store.insert_note("Harper likes espresso", "preference", "agent")
        hit = store.find_live_note_by_normalized(normalize_text("HARPER  likes espresso"))
        assert hit is not None and hit.id == note.id
        assert store.find_live_note_by_normalized("something else") is None
    finally:
        store.close()


def test_injected_clock_controls_created_at(tmp_path):
    store = make_store(tmp_path, now=lambda: "2026-08-20T00:00:00.000000+00:00")
    try:
        note = store.insert_note("clock test", "note", "agent")
        assert note.created_at == "2026-08-20T00:00:00.000000+00:00"
    finally:
        store.close()


def test_locked_db_raises_instead_of_hanging(tmp_path):
    store = make_store(tmp_path, busy_timeout_ms=100)
    blocker = sqlite3.connect(tmp_path / "memory.sqlite")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.insert_note("blocked write", "note", "agent")
    finally:
        blocker.rollback()
        blocker.close()
        store.close()
