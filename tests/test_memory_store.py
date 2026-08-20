# ABOUTME: Tests for MemoryStore: schema migration, append-only notes,
# ABOUTME: persistence across reopen, and normalized-duplicate lookup.
import sqlite3

import pytest

from reachy_openai_realtime.memory.store import MemoryStore, fts_match_expression, normalize_text


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


def test_search_finds_fts_match_and_skips_tombstones(tmp_path):
    store = make_store(tmp_path)
    try:
        kept = store.insert_note("Harper repairs the robot on Tuesdays", "fact", "agent")
        gone = store.insert_note("Old robot fact to delete", "fact", "agent")
        assert store.tombstone_note(gone.id) is True
        hits = store.search(fts_match_expression("robot"), limit=10)
        ids = [hit.id for hit in hits]
        assert kept.id in ids
        assert gone.id not in ids
    finally:
        store.close()


def test_search_hits_summary_text_too(tmp_path):
    store = make_store(tmp_path)
    try:
        summary = store.insert_summary(None, 1, "Espresso rituals in the garage", "a", "b")
        hits = store.search(fts_match_expression("espresso"), limit=10)
        assert any(hit.id == summary.id and hit.entry_type == "summary" for hit in hits)
        assert all(hit.kind == "summary" for hit in hits if hit.entry_type == "summary")
    finally:
        store.close()


def test_pinned_outranks_unpinned_on_equal_match(tmp_path):
    store = make_store(tmp_path)
    try:
        plain = store.insert_note("banjo practice on Sunday", "note", "agent")
        starred = store.insert_note("banjo practice on Monday", "note", "agent")
        assert store.set_pinned(starred.id, True) is True
        hits = store.search(fts_match_expression("banjo practice"), limit=10)
        assert hits[0].id == starred.id
        assert plain.id in [hit.id for hit in hits]
    finally:
        store.close()


def test_recent_outranks_old_on_equal_match(tmp_path):
    clock = {"value": "2026-01-01T00:00:00.000000+00:00"}
    store = make_store(tmp_path, now=lambda: clock["value"])
    try:
        old = store.insert_note("kazoo solo in the kitchen", "note", "agent")
        clock["value"] = "2026-08-20T00:00:00.000000+00:00"
        fresh = store.insert_note("kazoo solo in the hallway", "note", "agent")
        hits = store.search(fts_match_expression("kazoo solo"), limit=10)
        assert [hit.id for hit in hits][:2] == [fresh.id, old.id]
    finally:
        store.close()


def test_tombstone_stales_full_ancestor_chain(tmp_path):
    store = make_store(tmp_path)
    try:
        root = store.insert_summary(None, 3, "root", "a", "b")
        mid = store.insert_summary(root.id, 2, "mid", "a", "b")
        leaf = store.insert_summary(mid.id, 1, "leaf", "a", "b")
        note = store.insert_note("doomed fact", "fact", "agent")
        store.mark_summarized([note.id], leaf.id)
        store.tombstone_note(note.id)
        stale_ids = [summary.id for summary in store.stale_summaries()]
        assert stale_ids == [leaf.id, mid.id, root.id]  # ascending level
    finally:
        store.close()


def test_tree_queries(tmp_path):
    store = make_store(tmp_path)
    try:
        assert store.root_summary() is None
        root = store.insert_summary(None, 2, "root", "a", "b")
        child = store.insert_summary(root.id, 1, "child", "a", "b")
        note = store.insert_note("covered fact", "fact", "agent")
        store.mark_summarized([note.id], child.id)
        assert store.root_summary().id == root.id
        assert [c.id for c in store.children_of(root.id)] == [child.id]
        assert [n.id for n in store.notes_covered_by(child.id)] == [note.id]
        assert store.count_unconsolidated() == 0
        extra = store.insert_note("pending fact", "fact", "agent")
        assert store.count_unconsolidated() == 1
        assert [n.id for n in store.unconsolidated_notes(10)] == [extra.id]
    finally:
        store.close()


def test_update_summary_clears_stale_and_reindexes_fts(tmp_path):
    store = make_store(tmp_path)
    try:
        summary = store.insert_summary(None, 1, "before text zanzibar", "a", "b")
        note = store.insert_note("x", "note", "agent")
        store.mark_summarized([note.id], summary.id)
        store.tombstone_note(note.id)
        assert store.count_stale() == 1
        store.update_summary(summary.id, "after text quixote", "a", "b")
        assert store.count_stale() == 0
        assert store.search(fts_match_expression("zanzibar"), limit=5) == []
        assert any(h.id == summary.id for h in store.search(fts_match_expression("quixote"), limit=5))
    finally:
        store.close()


def test_delete_summary_marks_parent_stale(tmp_path):
    store = make_store(tmp_path)
    try:
        root = store.insert_summary(None, 2, "root", "a", "b")
        child = store.insert_summary(root.id, 1, "child", "a", "b")
        store.delete_summary(child.id)
        assert store.get_summary(child.id) is None
        assert [s.id for s in store.stale_summaries()] == [root.id]
    finally:
        store.close()


def test_empty_match_expression_for_symbol_only_query():
    assert fts_match_expression("!!! ???") == ""
