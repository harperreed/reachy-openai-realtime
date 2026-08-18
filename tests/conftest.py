# ABOUTME: Shared test fixtures/helpers. drive_fsm walks a session state machine
# ABOUTME: to a target state through legal transitions only (exercises the table).
import asyncio
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace

import numpy as np

from reachy_openai_realtime.session.fsm import LEGAL_TRANSITIONS, SessionState, SessionStateMachine

# ---------------------------------------------------------------------------
# FakeRecorder — duck-typed EventRecorder stub for flight-recorder assertions
# ---------------------------------------------------------------------------


class FakeRecorder:
    """Captures record() calls as (event, fields) tuples for test assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def drive_fsm(fsm: SessionStateMachine, target: SessionState) -> None:
    """Walk fsm to target via a shortest legal path. Raises if unreachable."""
    if fsm.state is target:
        return
    frontier = deque([(fsm.state, [])])
    seen = {fsm.state}
    while frontier:
        state, path = frontier.popleft()
        for nxt in LEGAL_TRANSITIONS[state]:
            if nxt in seen:
                continue
            if nxt is target:
                for step in path + [nxt]:
                    assert fsm.transition(step, reason="test_drive")
                return
            seen.add(nxt)
            frontier.append((nxt, path + [nxt]))
    raise AssertionError(f"no legal path from {fsm.state} to {target}")


# ---------------------------------------------------------------------------
# Shared fake: FakeSpeakerMedia — single definition; import from here
# ---------------------------------------------------------------------------


class FakeSpeakerMedia:
    """Minimal push_audio_sample stub for SpeakerWorker tests."""

    def __init__(self) -> None:
        self.pushed: list[np.ndarray] = []

    def push_audio_sample(self, data: np.ndarray) -> None:
        self.pushed.append(data)


# ---------------------------------------------------------------------------
# Scripted Realtime transport helpers (used by chaos tests and later phases)
# ---------------------------------------------------------------------------


def realtime_event(type_: str, **attrs) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **attrs)


class _RecorderSession:
    def __init__(self) -> None:
        self.updates: list = []

    async def update(self, *, session) -> None:
        self.updates.append(session)


class _RecorderInputBuffer:
    def __init__(self) -> None:
        self.appended = 0
        self.committed = 0

    async def append(self, *, audio: str) -> None:
        self.appended += 1

    async def commit(self) -> None:
        self.committed += 1


class _RecorderResponse:
    def __init__(self) -> None:
        self.created: list = []
        self.cancelled: list[str | None] = []

    async def create(self, response=None) -> None:
        self.created.append(response)

    async def cancel(self, response_id: str | None = None) -> None:
        self.cancelled.append(response_id)


class _RecorderConversationItem:
    def __init__(self) -> None:
        self.created: list = []
        self.deleted: list[str] = []
        self.truncations: list = []

    async def create(self, **kwargs) -> None:
        self.created.append(kwargs)

    async def delete(self, *, item_id: str, **kwargs) -> None:
        self.deleted.append(item_id)

    async def truncate(self, **kwargs) -> None:
        self.truncations.append(kwargs)


class ScriptedConnection:
    """Fake Realtime connection: replays scripted server events, then fails or idles."""

    def __init__(
        self,
        events: list,
        *,
        raise_after: Exception | None = None,
        on_drained: Callable[[], None] | None = None,
    ) -> None:
        self._events = list(events)
        self._raise_after = raise_after
        self._on_drained = on_drained
        self.session = _RecorderSession()
        self.input_audio_buffer = _RecorderInputBuffer()
        self.response = _RecorderResponse()
        self.conversation = SimpleNamespace(item=_RecorderConversationItem())

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            await asyncio.sleep(0)
            yield event
        if self._on_drained is not None:
            self._on_drained()
        if self._raise_after is not None:
            raise self._raise_after
        while True:
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        pass


class FakeRealtimeClient:
    def __init__(self, connections: list[ScriptedConnection]) -> None:
        self._connections = list(connections)
        self.realtime = self

    def connect(self, *, model: str):
        assert self._connections, "scripted connections exhausted"
        connection = self._connections.pop(0)

        class _Ctx:
            async def __aenter__(_self):
                return connection

            async def __aexit__(_self, *exc_info):
                return False

        return _Ctx()
