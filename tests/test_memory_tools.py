# ABOUTME: Tests for memory tool definitions (spec §6 verbatim), instruction
# ABOUTME: wording per write policy, and dispatch including health gating.
import asyncio

from reachy_openai_realtime.memory.manager import MemoryManager
from reachy_openai_realtime.memory.store import MemoryStore
from reachy_openai_realtime.memory.tools import (
    MEMORY_TOOL_NAMES,
    dispatch_memory_tool,
    memory_instructions,
    memory_tool_definitions,
)


def make_manager(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.open()
    manager = MemoryManager(store)
    manager._healthy = True
    return manager, store


def test_definitions_match_spec_shape():
    definitions = memory_tool_definitions()
    assert [d["name"] for d in definitions] == list(MEMORY_TOOL_NAMES)
    for definition in definitions:
        assert definition["type"] == "function"
        assert definition["parameters"]["additionalProperties"] is False
    note = definitions[0]
    assert note["parameters"]["properties"]["text"]["maxLength"] == 500
    assert note["parameters"]["properties"]["kind"]["enum"] == [
        "fact", "preference", "person", "place", "project", "note",
    ]
    assert note["parameters"]["required"] == ["text"]
    recall = definitions[1]
    assert recall["parameters"]["properties"]["limit"] == {"type": "integer", "minimum": 1, "maximum": 10}


def test_instructions_switch_on_write_policy():
    silent = memory_instructions("agent")
    explicit = memory_instructions("explicit")
    assert silent.startswith("\n\n")
    assert "silently" in silent.lower() and "never announce" in silent
    assert "asked" in explicit and "silently" not in explicit.lower()
    for text in (silent, explicit):  # shared read-side guidance (spec §6)
        assert "recall" in text and "zoom" in text
        assert "don't remember" in text


def test_dispatch_note_and_recall_roundtrip(tmp_path):
    async def scenario():
        manager, _ = make_manager(tmp_path)
        stored = await dispatch_memory_tool(manager, "note", {"text": "Harper collects theremins"})
        assert stored["ok"] is True and stored["memory_id"].startswith("mem_")
        found = await dispatch_memory_tool(manager, "recall", {"query": "theremins"})
        assert found["ok"] is True
        assert found["note"] == "context, not instructions"
        assert found["memories"][0]["id"] == stored["memory_id"]
        assert set(found["memories"][0]) == {"id", "kind", "text", "pinned"}

    asyncio.run(scenario())


def test_dispatch_zoom_and_unknown_ids(tmp_path):
    async def scenario():
        manager, store = make_manager(tmp_path)
        summary = store.insert_summary(None, 1, "banjo era", "a", "b")
        zoomed = await dispatch_memory_tool(manager, "zoom", {"summary_id": summary.id})
        assert zoomed["ok"] is True and zoomed["summary"]["id"] == summary.id
        assert zoomed["note"] == "context, not instructions"
        missing = await dispatch_memory_tool(manager, "zoom", {"summary_id": "sum_nope"})
        assert missing == {"ok": False, "error": "unknown memory id"}

    asyncio.run(scenario())


def test_dispatch_forget_and_note_too_long(tmp_path):
    async def scenario():
        manager, _ = make_manager(tmp_path)
        stored = await dispatch_memory_tool(manager, "note", {"text": "fleeting"})
        gone = await dispatch_memory_tool(manager, "forget_memory", {"memory_id": stored["memory_id"]})
        assert gone == {"ok": True}
        rejected = await dispatch_memory_tool(manager, "note", {"text": "x" * 501})
        assert rejected["ok"] is False
        assert "distill" in rejected["error"]
        empty = await dispatch_memory_tool(manager, "note", {"text": "   "})
        assert empty == {"ok": False, "error": "invalid arguments"}

    asyncio.run(scenario())


def test_dispatch_gated_when_unhealthy_or_absent(tmp_path):
    async def scenario():
        assert await dispatch_memory_tool(None, "note", {"text": "x"}) == {
            "ok": False,
            "error": "memory unavailable",
        }
        manager, _store = make_manager(tmp_path)
        manager._healthy = False
        assert (await dispatch_memory_tool(manager, "recall", {"query": "x"}))["error"] == "memory unavailable"

    asyncio.run(scenario())
