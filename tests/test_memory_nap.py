# ABOUTME: Tests for NapConsolidator: trigger gates, chunk consolidation,
# ABOUTME: roll-up, root rewrite, stale-first scrubbing, and abort safety.
import asyncio

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.memory.nap import NapConsolidator
from reachy_openai_realtime.memory.store import MemoryStore


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class EventRecorder:
    def __init__(self):
        self.events = []

    def __call__(self, event, **fields):
        self.events.append((event, fields))


class FakeSummarizer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def __call__(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("summarizer down")
        return f"summary of {len(texts)} entries"


def make_nap(tmp_path, **config_overrides):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.open()
    config = AppConfig(**config_overrides) if config_overrides else AppConfig()
    clock = FakeClock()
    events = EventRecorder()
    summarizer = FakeSummarizer()
    nap = NapConsolidator(store=store, summarize=summarizer, config=config, recorder=events, clock=clock)
    return nap, store, clock, events, summarizer


def seed_notes(store, count, prefix="fact"):
    return [store.insert_note(f"{prefix} number {i}", "fact", "agent") for i in range(count)]


def test_nap_gated_when_not_idle(tmp_path):
    async def scenario():
        nap, store, _, _, summarizer = make_nap(tmp_path)
        seed_notes(store, 25)
        written = await nap.evaluate_once(lambda: False)
        assert written == 0 and summarizer.calls == []

    asyncio.run(scenario())


def test_nap_gated_below_20_pending_and_no_stale(tmp_path):
    async def scenario():
        nap, store, _, _, summarizer = make_nap(tmp_path)
        seed_notes(store, 19)
        assert await nap.evaluate_once(lambda: True) == 0
        assert summarizer.calls == []

    asyncio.run(scenario())


def test_nap_consolidates_chunk_and_builds_root(tmp_path):
    async def scenario():
        nap, store, _, events, _summarizer = make_nap(tmp_path)
        seed_notes(store, 20)
        written = await nap.evaluate_once(lambda: True)
        assert written >= 1
        assert store.count_unconsolidated() == 0
        root = store.root_summary()
        assert root is not None
        assert root.text == "summary of 1 entries"  # root rewritten after the top-level change (spec §8 step 4)
        children = store.children_of(root.id)
        assert len(children) == 1 and children[0].level == 1
        assert store.notes_covered_by(children[0].id)  # summarized_by stamped
        names = [event for event, _ in events.events]
        assert "memory.nap.started" in names and "memory.nap.completed" in names

    asyncio.run(scenario())


def test_partial_chunk_stays_pending(tmp_path):
    async def scenario():
        nap, store, _, _, _ = make_nap(tmp_path)
        seed_notes(store, 30)
        await nap.evaluate_once(lambda: True)
        assert store.count_unconsolidated() == 10  # only full chunks of 20 consolidate

    asyncio.run(scenario())


def test_interval_floor_between_naps(tmp_path):
    async def scenario():
        nap, store, clock, _, _ = make_nap(tmp_path)
        seed_notes(store, 20)
        assert await nap.evaluate_once(lambda: True) >= 1
        seed_notes(store, 20, prefix="later")
        clock.now = 899.0
        assert await nap.evaluate_once(lambda: True) == 0
        clock.now = 901.0
        assert await nap.evaluate_once(lambda: True) >= 1

    asyncio.run(scenario())


def test_stale_rewrite_runs_before_new_consolidation(tmp_path):
    async def scenario():
        nap, store, clock, _, summarizer = make_nap(tmp_path)
        notes = seed_notes(store, 20)
        await nap.evaluate_once(lambda: True)
        store.tombstone_note(notes[0].id)  # stales leaf + root
        assert store.count_stale() >= 1
        clock.now = 1000.0
        await nap.evaluate_once(lambda: True)
        assert store.count_stale() == 0
        # rewrite summarized the 19 SURVIVING notes, not 20:
        assert any(len(call) == 19 for call in summarizer.calls)

    asyncio.run(scenario())


def test_zero_survivor_stale_node_is_deleted(tmp_path):
    async def scenario():
        nap, store, clock, _, _ = make_nap(tmp_path)
        notes = seed_notes(store, 20)
        await nap.evaluate_once(lambda: True)
        for note in notes:
            store.tombstone_note(note.id)
        clock.now = 1000.0
        await nap.evaluate_once(lambda: True)
        root = store.root_summary()
        assert root is None or store.children_of(root.id) == []

    asyncio.run(scenario())


def test_rollup_at_branching_factor(tmp_path):
    async def scenario():
        nap, store, clock, _, _ = make_nap(tmp_path, memory_nap_max_nodes=50)
        for round_index in range(8):
            seed_notes(store, 20, prefix=f"round{round_index}")
            clock.now = (round_index + 1) * 1000.0
            await nap.evaluate_once(lambda: True)
        root = store.root_summary()
        children = store.children_of(root.id)
        levels = sorted(child.level for child in children)
        assert 2 in levels  # 8 level-1 siblings rolled up into a level-2 node
        assert levels.count(1) < 8

    asyncio.run(scenario())


def test_max_nodes_bounds_one_nap(tmp_path):
    async def scenario():
        nap, store, _, _, _summarizer = make_nap(tmp_path, memory_nap_max_nodes=2)
        seed_notes(store, 100)
        written = await nap.evaluate_once(lambda: True)
        assert written <= 2

    asyncio.run(scenario())


def test_abort_mid_nap_leaves_consistent_db(tmp_path):
    async def scenario():
        nap, store, clock, _, _ = make_nap(tmp_path)
        seed_notes(store, 60)
        calls = {"count": 0}

        def flaky_idle():
            calls["count"] += 1
            return calls["count"] <= 2  # idle for the gate + first node, then conversation resumes

        await nap.evaluate_once(flaky_idle)
        consolidated = 60 - store.count_unconsolidated()
        assert consolidated in (0, 20, 40)  # whole chunks only, never a torn chunk
        clock.now = 2000.0
        await nap.evaluate_once(lambda: True)  # next idle window resumes where the abort left off
        assert store.count_unconsolidated() == 0

    asyncio.run(scenario())


def test_summarizer_failure_emits_error_and_leaves_pending(tmp_path):
    async def scenario():
        store = MemoryStore(tmp_path / "memory.sqlite")
        store.open()
        events = EventRecorder()
        nap = NapConsolidator(
            store=store,
            summarize=FakeSummarizer(fail=True),
            config=AppConfig(),
            recorder=events,
            clock=FakeClock(),
        )
        seed_notes(store, 20)
        written = await nap.evaluate_once(lambda: True)
        assert written == 0
        assert store.count_unconsolidated() == 20
        assert any(event == "memory.error" and fields == {"operation": "nap"} for event, fields in events.events)

    asyncio.run(scenario())


def test_summarizer_output_clamped_to_1000_chars(tmp_path):
    async def scenario():
        store = MemoryStore(tmp_path / "memory.sqlite")
        store.open()

        class Verbose:
            async def __call__(self, texts):
                return "x" * 5000

        nap = NapConsolidator(
            store=store,
            summarize=Verbose(),
            config=AppConfig(),
            recorder=EventRecorder(),
            clock=FakeClock(),
        )
        seed_notes(store, 20)
        await nap.evaluate_once(lambda: True)
        root = store.root_summary()
        for summary in [root] + store.children_of(root.id):
            assert len(summary.text) <= 1000

    asyncio.run(scenario())
