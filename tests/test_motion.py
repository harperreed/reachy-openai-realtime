from __future__ import annotations

from typing import Any

import pytest

from reachy_japanese_realtime.motion import MotionController


class FakeRobot:
    def __init__(self) -> None:
        self.cancelled = False
        self.targets: list[dict[str, Any]] = []

    def goto_target(self, **kwargs: Any) -> None:
        self.targets.append(kwargs)

    def cancel_move(self) -> None:
        self.cancelled = True


def test_count_is_clamped() -> None:
    command = MotionController.validate("nod", {"count": 99})
    assert command.arguments == {"count": 3}


def test_invalid_direction_is_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        MotionController.validate("look", {"direction": "behind"})


def test_raw_angles_are_not_accepted() -> None:
    with pytest.raises(ValueError, match="unknown"):
        MotionController.validate("set_motor_angles", {"angles": [999]})


def test_stop_cancels_robot() -> None:
    robot = FakeRobot()
    controller = MotionController(robot)
    result = controller.submit("stop_motion", {})
    assert result["ok"] is True
    assert robot.cancelled is True
