# ABOUTME: Integration tests for memory wiring in RealtimeRobotSession:
# ABOUTME: tool advertising gated on health, wake-block injection, nap idle probe.
import asyncio
import time

from reachy_openai_realtime.config import AppConfig
from reachy_openai_realtime.memory.manager import MemoryManager
from reachy_openai_realtime.memory.store import MemoryStore
from reachy_openai_realtime.memory.tools import MEMORY_TOOL_NAMES
from reachy_openai_realtime.realtime import RealtimeRobotSession
from reachy_openai_realtime.session.fsm import SessionState, SessionStateMachine


class FakeMotion:
    def tool_definitions(self):
        return [{"type": "function", "name": "wave", "description": "", "parameters": {}}]

    def emotion_names(self):
        return []

    def dance_names(self):
        return []


def make_bare_session(tmp_path, healthy=True):
    store = MemoryStore(tmp_path / "memory.sqlite")
    store.open()
    manager = MemoryManager(store)
    manager._healthy = healthy
    session = RealtimeRobotSession.__new__(RealtimeRobotSession)
    session.config = AppConfig()
    session.motion = FakeMotion()
    session.memory = manager
    session._language_provider = None
    session._wake_block = ""
    session._memory_tools_active = False
    session.fsm = SessionStateMachine()
    session._last_fsm_transition_at = time.monotonic()
    return session, store, manager


def session_config_of(session):
    # _session_config may need more bare fields depending on the file's current
    # body (audio formats, voice, etc. all come from session.config); add any
    # missing plain attributes the same way make_bare_session does.
    return session._session_config()


def test_memory_tools_and_instructions_present_when_active(tmp_path):
    session, _store, _ = make_bare_session(tmp_path)
    session._memory_tools_active = True
    config = session_config_of(session)
    tool_names = [tool["name"] for tool in config["tools"]]
    for name in MEMORY_TOOL_NAMES:
        assert name in tool_names
    assert "wave" in tool_names
    assert "recall" in config["instructions"]


def test_memory_tools_absent_when_inactive(tmp_path):
    session, _, _ = make_bare_session(tmp_path)
    config = session_config_of(session)
    tool_names = [tool["name"] for tool in config["tools"]]
    for name in MEMORY_TOOL_NAMES:
        assert name not in tool_names


def test_wake_block_prefixes_instructions(tmp_path):
    session, _, _ = make_bare_session(tmp_path)
    session._memory_tools_active = True
    session._wake_block = "Background Reachy remembers hearing around it. TEST-MARKER"
    config = session_config_of(session)
    assert config["instructions"].startswith(session._wake_block)


def test_write_policy_switches_instruction_wording(tmp_path):
    session, _, _ = make_bare_session(tmp_path)
    session._memory_tools_active = True
    session.config = AppConfig(memory_write_policy="explicit")
    explicit_config = session_config_of(session)
    session.config = AppConfig(memory_write_policy="agent")
    agent_config = session_config_of(session)
    assert "never announce" in agent_config["instructions"]
    assert "never announce" not in explicit_config["instructions"]
    assert "asked Reachy to remember" in explicit_config["instructions"]


def test_memory_tool_handler_routes_to_dispatch(tmp_path):
    async def scenario():
        session, _, manager = make_bare_session(tmp_path)
        handler = session._memory_tool_handler("note")
        result = await handler({"text": "wired up"})
        assert result["ok"] is True
        manager._healthy = False
        gated = await session._memory_tool_handler("recall")({"query": "x"})
        assert gated == {"ok": False, "error": "memory unavailable"}

    asyncio.run(scenario())


def test_nap_idle_probe(tmp_path):
    session, _, _ = make_bare_session(tmp_path)
    session.fsm._state = SessionState.LISTENING
    session._last_fsm_transition_at = time.monotonic() - 500.0
    assert session._nap_idle() is True
    session._last_fsm_transition_at = time.monotonic()
    assert session._nap_idle() is False
    session.fsm._state = SessionState.ASSISTANT_SPEAKING
    session._last_fsm_transition_at = time.monotonic() - 500.0
    assert session._nap_idle() is False
