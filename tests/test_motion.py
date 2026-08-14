from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from reachy_mini.utils import create_head_pose

from reachy_openai_realtime.motion import (
    IdleBreathingMotion,
    ListeningNodMotion,
    MotionController,
    SpeakingMotion,
)


class FakeRobot:
    def __init__(self) -> None:
        self.cancelled = False
        self.targets: list[dict[str, Any]] = []

    def goto_target(self, **kwargs: Any) -> None:
        self.targets.append(kwargs)

    def cancel_move(self) -> None:
        self.cancelled = True

    def set_target(self, **kwargs: Any) -> None:
        self.targets.append(kwargs)

    def get_current_head_pose(self) -> np.ndarray:
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        return ([0.0] * 7, [0.0, 0.0])


def test_count_is_clamped() -> None:
    command = MotionController.validate("nod", {"count": 99})
    assert command.arguments == {"count": 3}


def test_invalid_direction_is_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        MotionController.validate("look", {"direction": "behind"})


def test_raw_angles_are_not_accepted() -> None:
    with pytest.raises(ValueError, match="unknown"):
        MotionController.validate("set_motor_angles", {"angles": [999]})


def test_stop_motion_preserves_wireless_media_pipeline() -> None:
    robot = FakeRobot()
    controller = MotionController(robot)
    result = controller.submit("stop_motion", {})
    assert result["ok"] is True
    assert robot.cancelled is False


def test_idle_breathing_matches_official_amplitudes() -> None:
    motion = IdleBreathingMotion(
        create_head_pose(0, 0, 0, 0, 0, 0, degrees=True),
        [0.0, 0.0],
    )

    head, antennas, body_yaw = motion.evaluate(1.5)

    assert head[2, 3] == pytest.approx(0.005 * np.sin(0.1 * np.pi))
    np.testing.assert_allclose(antennas, np.deg2rad([15.0, -15.0]))
    assert body_yaw is None


def test_idle_breathing_interpolates_toward_neutral() -> None:
    start_head = create_head_pose(0, 0, 0, 0, 12, 0, degrees=True)
    motion = IdleBreathingMotion(start_head, [0.2, -0.2])

    head, antennas, _ = motion.evaluate(0.5)

    assert head.shape == (4, 4)
    expected_antennas = 0.5 * (
        np.array([0.2, -0.2]) + np.deg2rad([-10.0, 10.0])
    )
    np.testing.assert_allclose(antennas, expected_antennas, atol=1e-4)


def test_idle_breathing_preserves_persistent_look_direction() -> None:
    base_head = create_head_pose(yaw=-22, degrees=True)
    motion = IdleBreathingMotion(
        base_head,
        [0.0, 0.0],
        base_head=base_head,
        interpolation_duration=0.0,
    )

    head, _, body_yaw = motion.evaluate(1.5)

    z_offset = 0.005 * np.sin(2.0 * np.pi * 0.1 * 1.5)
    expected = base_head @ create_head_pose(z=z_offset, degrees=True, mm=False)
    np.testing.assert_allclose(head, expected)
    assert body_yaw is None


def test_idle_breathing_stops_when_disabled() -> None:
    robot = FakeRobot()
    controller = MotionController(robot)
    controller._idle_start_delay = 0.0
    controller.set_idle_enabled(True)

    controller._update_idle_motion()
    assert len(robot.targets) == 1

    controller.set_idle_enabled(False)
    controller._update_idle_motion()
    assert len(robot.targets) == 1


def test_listening_nod_runs_once_then_stays_neutral() -> None:
    motion = ListeningNodMotion(
        create_head_pose(0, 0, 0, 0, 0, 0, degrees=True),
        interpolation_duration=0.0,
        nod_duration=0.5,
        max_pitch_degrees=5.0,
    )

    np.testing.assert_allclose(
        motion.evaluate(0.0),
        create_head_pose(pitch=0, degrees=True),
    )
    np.testing.assert_allclose(
        motion.evaluate(0.25),
        create_head_pose(pitch=5, degrees=True),
    )
    np.testing.assert_allclose(
        motion.evaluate(1.0),
        create_head_pose(pitch=0, degrees=True),
        atol=1e-12,
    )
    assert motion.is_moving(0.25) is True
    assert motion.is_moving(1.0) is False
    assert motion.is_moving(2.25) is False


def test_listening_nod_returns_to_persistent_look_direction() -> None:
    base_head = create_head_pose(yaw=-22, degrees=True)
    motion = ListeningNodMotion(
        base_head,
        base_head=base_head,
        interpolation_duration=0.0,
        nod_duration=0.5,
    )

    np.testing.assert_allclose(motion.evaluate(1.0), base_head, atol=1e-12)


def test_listening_nod_runs_only_while_enabled() -> None:
    robot = FakeRobot()
    controller = MotionController(robot)

    controller.set_listening_enabled(True)
    controller._update_ambient_motion()
    targets_while_listening = len(robot.targets)
    assert targets_while_listening == 1

    controller.set_listening_enabled(False)
    controller._update_ambient_motion()
    assert len(robot.targets) == targets_while_listening + 1


def test_listening_nod_does_not_send_motor_commands_during_quiet_window() -> None:
    robot = FakeRobot()
    controller = MotionController(robot)
    controller.set_listening_enabled(True)
    controller._listening_motion = ListeningNodMotion(
        robot.get_current_head_pose(),
        interpolation_duration=0.0,
        nod_duration=0.45,
    )
    controller._listening_started_at = controller._last_activity_at - 1.0
    controller._listening_was_moving = True

    controller._update_listening_motion()
    targets_after_entering_quiet_window = len(robot.targets)
    controller._update_listening_motion()

    assert targets_after_entering_quiet_window == 1
    assert len(robot.targets) == targets_after_entering_quiet_window


def test_speaking_motion_is_subtle_and_changes_over_time() -> None:
    motion = SpeakingMotion(
        create_head_pose(0, 0, 0, 0, 0, 0, degrees=True),
        [0.0, 0.0],
        interpolation_duration=0.0,
    )

    head_a, antennas_a = motion.evaluate(0.0)
    head_b, antennas_b = motion.evaluate(0.7)

    assert not np.allclose(head_a, head_b)
    assert not np.allclose(antennas_a, antennas_b)
    assert np.max(np.abs(antennas_b)) <= np.deg2rad(16.0)
    assert abs(head_b[2, 3]) <= 0.0021


def test_speaking_motion_is_composed_on_persistent_look_direction() -> None:
    base_head = create_head_pose(yaw=-22, degrees=True)
    motion = SpeakingMotion(
        base_head,
        [0.0, 0.0],
        base_head=base_head,
        interpolation_duration=0.0,
    )

    head, _ = motion.evaluate(0.0)

    pitch = 0.6 * np.sin(0.4)
    yaw = 2.6 * np.sin(0.8)
    expected = base_head @ create_head_pose(
        roll=0.0,
        pitch=pitch,
        yaw=yaw,
        degrees=True,
        mm=False,
    )
    np.testing.assert_allclose(head, expected)


def test_look_direction_becomes_base_for_following_motions() -> None:
    robot = FakeRobot()
    controller = MotionController(robot)

    controller._execute(MotionController.validate("look", {"direction": "right"}))
    expected = create_head_pose(yaw=-22, degrees=True)
    np.testing.assert_allclose(controller._get_base_head(), expected)

    controller._speaking_motion = SpeakingMotion(
        expected,
        [0.0, 0.0],
        base_head=expected,
    )
    controller._speaking_started_at = 0.0
    controller.set_speaking_enabled(False)
    controller._update_ambient_motion()

    np.testing.assert_allclose(robot.targets[-1]["head"], expected)


def test_speaking_motion_runs_only_while_enabled() -> None:
    robot = FakeRobot()
    controller = MotionController(robot)

    controller.set_speaking_enabled(True)
    controller._update_ambient_motion()
    targets_while_speaking = len(robot.targets)
    assert targets_while_speaking == 1

    controller.set_speaking_enabled(False)
    controller._update_ambient_motion()
    assert len(robot.targets) == targets_while_speaking + 1
