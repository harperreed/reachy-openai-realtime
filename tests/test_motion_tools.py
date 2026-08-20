# ABOUTME: Tests for the recorded-move tool surface — submit routing, catalog
# ABOUTME: validation, targeted stops, and availability-gated tool definitions.
import time

import numpy as np
import pytest
from reachy_mini.utils import create_head_pose

from reachy_openai_realtime.motion import (
    EMOTIONS_DATASET,
    TOOL_DEFINITIONS,
    MotionManager,
    RecordedMoveCatalog,
)


class FakeRobot:
    def __init__(self) -> None:
        self.targets: list[dict] = []
        self.gotos: list[dict] = []

    def set_target(self, **kwargs) -> None:
        self.targets.append(kwargs)

    def goto_target(self, **kwargs) -> None:
        self.gotos.append(kwargs)

    def get_current_head_pose(self) -> np.ndarray:
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        return ([0.0] * 7, [0.0, 0.0])


class FakeMove:
    def __init__(self, duration: float = 0.2) -> None:
        self.duration = duration

    def evaluate(self, t: float):
        if t >= self.duration:
            raise Exception("beyond duration")  # noqa: TRY002
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True), np.array([0.0, 0.0]), None


class FakeRecordedMoves:
    def __init__(self, names_to_moves: dict) -> None:
        self._moves = names_to_moves

    def list_moves(self) -> list[str]:
        return list(self._moves)

    def get(self, name: str):
        return self._moves[name]


def ready_catalog(names_to_moves: dict) -> RecordedMoveCatalog:
    catalog = RecordedMoveCatalog(EMOTIONS_DATASET, loader=lambda ds: FakeRecordedMoves(names_to_moves))
    catalog.load_async()
    assert catalog.wait_ready(timeout=2.0)
    return catalog


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_play_emotion_routes_through_catalog_and_manager() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove()}))
    manager.start()
    result = manager.submit("play_emotion", {"emotion": "happy1"})
    assert result["ok"] is True and result["emotion"] == "happy1" and result["duration_s"] == 0.2
    manager.close()


def test_play_emotion_unknown_name_raises_value_error() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove()}))
    with pytest.raises(ValueError, match="unknown move"):
        manager.submit("play_emotion", {"emotion": "nonexistent"})


def test_play_emotion_without_catalog_returns_unavailable() -> None:
    manager = MotionManager(FakeRobot())  # no catalogs
    result = manager.submit("play_emotion", {"emotion": "happy1"})
    assert result == {"ok": False, "error": "emotion catalog unavailable"}


def test_play_dance_while_catalog_loading_returns_unavailable() -> None:
    slow = RecordedMoveCatalog(EMOTIONS_DATASET, loader=lambda ds: FakeRecordedMoves({}))
    # never load_async'd -> stays "loading"
    manager = MotionManager(FakeRobot(), dances=slow)
    result = manager.submit("play_dance", {"dance": "spin"})
    assert result["ok"] is False and "unavailable" in result["error"]


def test_stop_emotion_stops_only_active_emotion() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"long": FakeMove(duration=10.0)}))
    manager.start()
    assert manager.submit("play_emotion", {"emotion": "long"})["ok"] is True
    assert wait_until(lambda: manager._current is not None)
    result = manager.submit("stop_emotion", {})
    assert result == {"ok": True, "motion": "stop_emotion", "stopped": True}
    idle_stop = manager.submit("stop_dance", {})
    assert idle_stop == {"ok": True, "motion": "stop_dance", "stopped": False}
    manager.close()


def test_stop_emotion_does_not_clear_background_enables() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"long": FakeMove(duration=10.0)}))
    manager.set_idle_enabled(True)
    manager.start()
    manager.submit("play_emotion", {"emotion": "long"})
    manager.submit("stop_emotion", {})
    assert manager._idle_enabled.is_set()
    manager.close()


def test_tool_definitions_gate_on_catalog_availability() -> None:
    bare = MotionManager(FakeRobot())
    names = [tool["name"] for tool in bare.tool_definitions()]
    assert names == [tool["name"] for tool in TOOL_DEFINITIONS]

    with_emotions = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove()}))
    names = [tool["name"] for tool in with_emotions.tool_definitions()]
    assert "play_emotion" in names and "stop_emotion" in names
    assert "play_dance" not in names


def test_play_tool_schemas_enumerate_catalog_names() -> None:
    """The model picks names straight from the schema; an open string invites
    invented names and leaves the model preferring the enum'd express tool."""
    manager = MotionManager(
        FakeRobot(),
        emotions=ready_catalog({"happy1": FakeMove(), "sad2": FakeMove()}),
    )
    play = next(t for t in manager.tool_definitions() if t["name"] == "play_emotion")
    assert play["parameters"]["properties"]["emotion"]["enum"] == ["happy1", "sad2"]

    from reachy_openai_realtime.motion.tools import RECORDED_MOVE_TOOL_DEFINITIONS

    pristine = next(
        t for t in RECORDED_MOVE_TOOL_DEFINITIONS["emotion"] if t["name"] == "play_emotion"
    )
    assert "enum" not in pristine["parameters"]["properties"]["emotion"]


def test_emotion_names_lists_catalog() -> None:
    manager = MotionManager(FakeRobot(), emotions=ready_catalog({"happy1": FakeMove(), "sad2": FakeMove()}))
    assert manager.emotion_names() == ["happy1", "sad2"]
    assert manager.dance_names() == []
