# ABOUTME: Tests for MemoryManager: caps, dedup, health gating, events,
# ABOUTME: and wake-block assembly with pinned-first truncation.
import asyncio

import pytest

from reachy_openai_realtime.memory.manager import (
    WAKE_FRAMING,
    MemoryManager,
    MemoryUnavailableError,
    NoteTooLongError,
    UnknownMemoryIdError,
)
from reachy_openai_realtime.memory.store import MemoryStore


class EventRecorder:
    def __init__(self):
        self.events = []

    def __call__(self, event, **fields):
        self.events.append((event, fields))


def make_manager(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.open()
    events = EventRecorder()
    manager = MemoryManager(store, recorder=events)
    manager._healthy = True  # store opened synchronously above
    return manager, store, events


def test_note_stores_and_emits_created_without_text(tmp_path):
    async def scenario():
        manager, _store, events = make_manager(tmp_path)
        note = await manager.note("Harper naps at 15:00", kind="fact")
        assert note.id.startswith("mem_")
        event, fields = events.events[-1]
        assert event == "memory.created"
        assert fields == {"memory_id": note.id, "kind": "fact", "source": "agent"}

    asyncio.run(scenario())


def test_explicit_flag_sets_source(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        note = await manager.note("remember the garage code story", explicit=True)
        assert note.source == "user_explicit"

    asyncio.run(scenario())


def test_note_over_500_chars_raises_retryable_error(tmp_path):
    async def scenario():
        manager, store, _ = make_manager(tmp_path)
        with pytest.raises(NoteTooLongError):
            await manager.note("x" * 501)
        assert store.count_notes() == 0  # nothing inserted, nothing truncated

    asyncio.run(scenario())


def test_duplicate_note_touches_and_returns_existing_id(tmp_path):
    async def scenario():
        manager, _, events = make_manager(tmp_path)
        first = await manager.note("Harper likes espresso")
        second = await manager.note("  harper   LIKES espresso ")
        assert second.id == first.id
        assert events.events[-1][0] == "memory.updated"

    asyncio.run(scenario())


def test_recall_empty_query_returns_empty_and_never_fabricates(tmp_path):
    async def scenario():
        manager, _, events = make_manager(tmp_path)
        assert await manager.recall("!!!") == []
        hits = await manager.recall("anything at all")
        assert hits == []
        assert events.events[-1][0] == "memory.recalled"
        assert events.events[-1][1]["count"] == 0

    asyncio.run(scenario())


def test_recall_touches_returned_notes(tmp_path):
    async def scenario():
        manager, store, _ = make_manager(tmp_path)
        note = await manager.note("theremin lessons on Friday")
        hits = await manager.recall("theremin")
        assert [hit.id for hit in hits] == [note.id]
        assert store.get_note(note.id).last_used_at is not None

    asyncio.run(scenario())


def test_zoom_unknown_id_errors_cleanly(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        with pytest.raises(UnknownMemoryIdError):
            await manager.zoom("sum_nope")

    asyncio.run(scenario())


def test_zoom_returns_children_capped(tmp_path):
    async def scenario():
        manager, store, _ = make_manager(tmp_path)
        root = store.insert_summary(None, 2, "root", "a", "b")
        children = [store.insert_summary(root.id, 1, f"child {i}", "a", "b") for i in range(10)]
        node, child_summaries, notes = await manager.zoom(root.id)
        assert node.id == root.id
        assert len(child_summaries) == 8  # zoom_child_cap default
        assert [c.id for c in child_summaries] == [c.id for c in children[:8]]
        assert notes == []

    asyncio.run(scenario())


def test_forget_tombstones_note_and_rejects_summaries(tmp_path):
    async def scenario():
        manager, store, events = make_manager(tmp_path)
        note = await manager.note("temporary fact")
        assert await manager.forget(note.id) is True
        assert store.get_note(note.id) is None
        assert events.events[-1][0] == "memory.deleted"
        summary = store.insert_summary(None, 1, "s", "a", "b")
        with pytest.raises(UnknownMemoryIdError):
            await manager.forget(summary.id)
        with pytest.raises(UnknownMemoryIdError):
            await manager.forget("mem_missing")

    asyncio.run(scenario())


def test_wake_block_frames_pins_first_and_respects_budget(tmp_path):
    async def scenario():
        manager, store, _ = make_manager(tmp_path)
        assert await manager.wake_block(2000) == ""  # empty memory → no block
        pinned = await manager.note("Harper answers to Doctor Biz", kind="person")
        store.set_pinned(pinned.id, True)
        store.insert_summary(None, 1, "Long summary text " * 40, "a", "b")
        block = await manager.wake_block(400)
        assert block.startswith(WAKE_FRAMING)
        assert "Doctor Biz" in block  # pinned survives truncation first
        assert len(block) <= 400

    asyncio.run(scenario())


def test_sqlite_error_marks_unhealthy_and_emits_memory_error(tmp_path):
    async def scenario():
        manager, store, events = make_manager(tmp_path)
        store.close()  # every store call now raises sqlite3.ProgrammingError
        with pytest.raises(MemoryUnavailableError):
            await manager.note("doomed")
        assert manager.healthy() is False
        assert ("memory.error", {"operation": "note"}) in events.events

    asyncio.run(scenario())


def test_list_entries_newest_first_with_search_and_count(tmp_path):
    async def scenario():
        manager, _store, _ = make_manager(tmp_path)
        first = await manager.note("alpha fact about ukulele")
        second = await manager.note("beta fact about accordion")
        entries, total = await manager.list_entries()
        assert total == 2
        assert [entry.id for entry in entries] == [second.id, first.id]
        filtered, total = await manager.list_entries(query="ukulele")
        assert [entry.id for entry in filtered] == [first.id]
        assert total == 2  # count is total live notes, not the filtered count

    asyncio.run(scenario())


def test_set_pinned_emits_updated_and_rejects_unknown(tmp_path):
    async def scenario():
        manager, store, events = make_manager(tmp_path)
        note = await manager.note("pin me")
        assert await manager.set_pinned(note.id, True) is True
        assert store.get_note(note.id).pinned is True
        assert events.events[-1] == ("memory.updated", {"memory_id": note.id})
        with pytest.raises(UnknownMemoryIdError):
            await manager.set_pinned("mem_missing", True)

    asyncio.run(scenario())
