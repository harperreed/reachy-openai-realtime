from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest
from reachy_mini.utils import create_head_pose

from reachy_openai_realtime.motion import (
    IdleBreathingMotion,
    ListeningNodMotion,
    MotionManager,
    MotionPriority,
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
    command = MotionManager.validate("nod", {"count": 99})
    assert command.arguments == {"count": 3}


def test_invalid_direction_is_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        MotionManager.validate("look", {"direction": "behind"})


def test_raw_angles_are_not_accepted() -> None:
    with pytest.raises(ValueError, match="unknown"):
        MotionManager.validate("set_motor_angles", {"angles": [999]})


def test_stop_motion_preserves_wireless_media_pipeline() -> None:
    robot = FakeRobot()
    controller = MotionManager(robot)
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
    controller = MotionManager(robot)
    controller._idle_start_delay = 0.0
    controller.set_idle_enabled(True)

    controller._update_idle_motion()
    assert len(robot.targets) == 1

    controller.set_idle_enabled(False)
    controller._update_idle_motion()
    assert len(robot.targets) == 1


def test_idle_recenters_base_after_quiet_period() -> None:
    robot = FakeRobot()
    controller = MotionManager(robot)
    events: list[str] = []
    controller.attach_recorder(lambda event, **fields: events.append(event))
    controller._idle_start_delay = 0.0
    controller._recenter_delay = 0.0
    controller._set_base_head(create_head_pose(yaw=-22, degrees=True))
    controller.set_idle_enabled(True)

    controller._update_idle_motion()

    neutral = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
    np.testing.assert_allclose(controller._get_base_head(), neutral)
    assert controller._idle_motion is not None
    np.testing.assert_allclose(controller._idle_motion.base_head, neutral)
    assert "motion.recentered" in events


def test_idle_keeps_look_direction_before_recenter_delay() -> None:
    robot = FakeRobot()
    controller = MotionManager(robot)
    controller._idle_start_delay = 0.0
    controller._recenter_delay = 3600.0
    look_head = create_head_pose(yaw=22, degrees=True)
    controller._set_base_head(look_head)
    controller.set_idle_enabled(True)

    controller._update_idle_motion()

    np.testing.assert_allclose(controller._get_base_head(), look_head)


def test_recenter_skips_when_base_is_already_neutral() -> None:
    robot = FakeRobot()
    controller = MotionManager(robot)
    events: list[str] = []
    controller.attach_recorder(lambda event, **fields: events.append(event))
    controller._idle_start_delay = 0.0
    controller._recenter_delay = 0.0
    controller.set_idle_enabled(True)

    controller._update_idle_motion()
    generator = controller._idle_motion
    controller._update_idle_motion()

    assert events == []
    assert controller._idle_motion is generator


def test_start_uses_neutral_base_even_when_head_is_tilted() -> None:
    class TiltedRobot(FakeRobot):
        def get_current_head_pose(self) -> np.ndarray:
            return create_head_pose(0, 0, 0, 0, 0, -22, degrees=True)

    controller = MotionManager(TiltedRobot())
    controller.start()

    neutral = create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)
    np.testing.assert_allclose(controller._get_base_head(), neutral)
    controller.close()


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
    controller = MotionManager(robot)

    controller.set_listening_enabled(True)
    controller._update_ambient_motion()
    targets_while_listening = len(robot.targets)
    assert targets_while_listening == 1

    controller.set_listening_enabled(False)
    controller._update_ambient_motion()
    assert len(robot.targets) == targets_while_listening + 1


def test_listening_nod_does_not_send_motor_commands_during_quiet_window() -> None:
    robot = FakeRobot()
    controller = MotionManager(robot)
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
    controller = MotionManager(robot)

    controller._execute(MotionManager.validate("look", {"direction": "right"}))
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
    controller = MotionManager(robot)

    controller.set_speaking_enabled(True)
    controller._update_ambient_motion()
    targets_while_speaking = len(robot.targets)
    assert targets_while_speaking == 1

    controller.set_speaking_enabled(False)
    controller._update_ambient_motion()
    assert len(robot.targets) == targets_while_speaking + 1


class RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_lower_priority_submission_is_rejected_while_higher_runs() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    release = threading.Event()
    started = threading.Event()

    def slow_gesture() -> None:
        started.set()
        release.wait(timeout=2.0)

    accepted = manager._start_activity("nod", MotionPriority.GESTURE, slow_gesture)
    assert accepted == {"ok": True, "motion": "nod"}
    manager.start()
    assert started.wait(timeout=2.0)

    rejected = manager.submit("look", {"direction": "left"})
    assert rejected["ok"] is False
    assert "busy" in rejected["error"] and "priority 75" in rejected["error"]
    release.set()
    manager.close()


def test_equal_or_higher_priority_preempts_running_activity() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    first_started = threading.Event()
    first_cancelled = threading.Event()

    def first() -> None:
        first_started.set()
        # cooperative activity: exits promptly once preempted
        while not manager._cancel_event.is_set():
            time.sleep(0.005)
        first_cancelled.set()

    manager._start_activity("shake_head", MotionPriority.GESTURE, first)
    manager.start()
    assert first_started.wait(timeout=2.0)

    result = manager.submit("nod", {"count": 1})
    assert result["ok"] is True
    assert first_cancelled.wait(timeout=2.0)
    assert wait_until(lambda: ("motion.cancelled", {"motion": "shake_head", "reason": "preempted"}) in recorder.events)
    assert wait_until(lambda: any(e == "motion.completed" and f.get("motion") == "nod" for e, f in recorder.events))
    manager.close()


def test_stop_motion_cancels_and_always_wins() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    started = threading.Event()

    def running() -> None:
        started.set()
        while not manager._cancel_event.is_set():
            time.sleep(0.005)

    manager._start_activity("nod", MotionPriority.GESTURE, running)
    manager.start()
    assert started.wait(timeout=2.0)
    result = manager.submit("stop_motion", {})
    assert result == {"ok": True, "motion": "stop_motion"}
    assert wait_until(lambda: ("motion.cancelled", {"motion": "nod", "reason": "stop"}) in recorder.events)
    manager.close()


def test_background_enables_survive_a_foreground_activity() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.set_idle_enabled(True)
    result = manager.submit("nod", {"count": 1})
    assert result["ok"] is True
    # §12: recorded/explicit moves suppress background motion but do not clear it
    assert manager._idle_enabled.is_set()
    manager.close()


def test_motion_events_emitted_for_gesture_lifecycle() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    recorder = RecordingRecorder()
    manager.attach_recorder(recorder)
    manager.start()
    manager.submit("nod", {"count": 1})
    assert wait_until(lambda: any(e == "motion.completed" and f.get("motion") == "nod" for e, f in recorder.events))
    names = [e for e, _ in recorder.events]
    assert names.index("motion.started") < names.index("motion.completed")
    manager.close()


def test_recorder_absence_does_not_break_motion() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)  # no recorder attached
    manager.start()
    assert manager.submit("nod", {"count": 1})["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    manager.close()


def test_worker_loop_beats_heartbeat() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    beats: list[float] = []
    manager.set_heartbeat(lambda: beats.append(time.monotonic()))
    manager.start()
    assert wait_until(lambda: len(beats) >= 3)
    manager.close()


def test_heartbeat_exception_does_not_kill_worker() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)

    def broken() -> None:
        raise RuntimeError("health sink down")

    manager.set_heartbeat(broken)
    manager.start()
    assert manager.submit("nod", {"count": 1})["ok"] is True
    assert wait_until(lambda: len(robot.targets) > 0)
    manager.close()


def test_wake_acknowledge_perks_up_without_touching_media_pipeline() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    try:
        result = manager.wake_acknowledge()
        assert result == {"ok": True, "motion": "wake_acknowledge"}
        # Perk-up is a two-segment gesture (lift, then settle).
        assert wait_until(lambda: len(robot.targets) >= 2)
    finally:
        manager.close()
    # A wake gesture must never tear down the wireless media pipeline.
    assert robot.cancelled is False


def test_sleeping_pose_lowers_head_and_relaxes_antennas() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    try:
        result = manager.sleeping_pose()
        assert result == {"ok": True, "motion": "sleeping_pose"}
        assert wait_until(lambda: len(robot.targets) >= 1)
    finally:
        manager.close()
    # MotionManager.close() appends a shutdown recenter (antennas [0, 0]) as the final
    # target, so the sleeping pose is not targets[-1]. Identify it by its own antenna
    # signature (deg2rad([-20, 20]) = drooped outward, left negative / right positive),
    # independent of ordering or any ambient idle tick before close().
    assert any(
        t.get("antennas") is not None and np.allclose(t["antennas"], np.deg2rad([-20.0, 20.0]))
        for t in robot.targets
    ), "sleeping_pose should command drooped-outward antennas (deg2rad([-20, 20]))"
    # App-level rest pose, not the hardware tuck.
    assert robot.cancelled is False


def test_boot_motion_looks_around_then_centers() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    try:
        result = manager.boot_motion()
        assert result == {"ok": True, "motion": "boot_motion"}
        # Look up, right, left, then center = four segments.
        assert wait_until(lambda: len(robot.targets) >= 4)
    finally:
        manager.close()
    assert robot.cancelled is False


def test_connection_failed_motion_shakes_then_droops() -> None:
    robot = FakeRobot()
    manager = MotionManager(robot)
    manager.start()
    try:
        result = manager.connection_failed_motion()
        assert result == {"ok": True, "motion": "connection_failed_motion"}
        assert wait_until(lambda: len(robot.targets) >= 3)
    finally:
        manager.close()
    assert robot.cancelled is False
