# ABOUTME: Tests for recorded-move playback inside MotionManager — tick loop,
# ABOUTME: clamping, body_yaw pass-through, cancellation, and §18 events.
import time

import numpy as np
from reachy_mini.utils import create_head_pose

from reachy_openai_realtime.motion import MotionManager


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
    """SDK RecordedMove shape: duration + evaluate(t) -> (head, antennas_rad, body_yaw)."""

    def __init__(self, duration: float = 0.2) -> None:
        self.duration = duration
        self.evaluated_at: list[float] = []

    def evaluate(self, t: float):
        if t >= self.duration:
            raise Exception("evaluated beyond duration")  # noqa: TRY002 — mirrors SDK's bare raise
        self.evaluated_at.append(t)
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True), np.array([0.1, -0.1]), 0.25


class RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_play_recorded_emotion_ticks_and_completes() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    move = FakeMove(duration=0.2)

    result = manager.play_recorded("emotion", "happy1", move)
    assert result["ok"] is True and result["emotion"] == "happy1"
    assert result["duration_s"] == 0.2
    assert wait_until(lambda: ("emotion.completed" in [e for e, _ in recorder.events]))
    # initial goto to the first frame, then set_target ticks with body_yaw passed through
    assert robot.gotos, "expected an initial goto to the move's start pose"
    assert any(t.get("body_yaw") == 0.25 for t in robot.targets)
    # evaluate() was never called at/beyond duration (clamp works)
    assert all(t < move.duration for t in move.evaluated_at)
    started = [f for e, f in recorder.events if e == "emotion.started"]
    assert started and started[0]["emotion"] == "happy1"
    manager.close()


def test_dance_uses_dance_priority_and_events() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    result = manager.play_recorded("dance", "spin", FakeMove(duration=0.15))
    assert result["ok"] is True and result["dance"] == "spin"
    assert wait_until(lambda: any(e == "dance.completed" for e, _ in recorder.events))
    manager.close()


def test_equal_or_higher_recorded_priority_preempts() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    long_dance = FakeMove(duration=5.0)
    assert manager.play_recorded("dance", "marathon", long_dance)["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)  # dance is running
    replaced = manager.play_recorded("dance", "second", FakeMove())
    assert replaced["ok"] is True  # equal priority (65 >= 65) preempts — arbitration rule 1
    promoted = manager.play_recorded("emotion", "surprise", FakeMove(duration=0.1))
    assert promoted["ok"] is True  # EMOTION 70 >= DANCE 65
    manager.close()


def test_look_is_rejected_during_recorded_move() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    assert manager.play_recorded("emotion", "long", FakeMove(duration=5.0))["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    rejected = manager.submit("look", {"direction": "left"})
    assert rejected["ok"] is False and "busy" in rejected["error"]  # LOOK 45 < EMOTION 70
    manager.close()


def test_stop_cancels_recorded_move_and_emits_motion_cancelled() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    assert manager.play_recorded("dance", "marathon", FakeMove(duration=10.0))["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    manager.submit("stop_motion", {})
    assert wait_until(lambda: ("motion.cancelled", {"motion": "marathon", "reason": "stop"}) in recorder.events)
    assert not any(e == "dance.completed" for e, _ in recorder.events)
    manager.close()


def test_evaluate_exception_ends_move_without_killing_worker() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()

    class BrokenMove(FakeMove):
        def evaluate(self, t: float):
            raise RuntimeError("corrupt trajectory")

    assert manager.play_recorded("emotion", "broken", BrokenMove(duration=1.0))["ok"] is True
    # worker survives: a later gesture still executes
    assert wait_until(lambda: manager.submit("nod", {"count": 1})["ok"] is True)
    assert wait_until(lambda: len(robot.gotos) + len(robot.targets) > 0)
    manager.close()
