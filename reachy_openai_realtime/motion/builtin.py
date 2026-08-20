# ABOUTME: Built-in ambient motion generators — idle breathing, listening nod,
# ABOUTME: and speaking motion — used by the MotionManager arbitrator.
from __future__ import annotations

from typing import Any

import numpy as np
from reachy_mini.utils import create_head_pose
from reachy_mini.utils.interpolation import linear_pose_interpolation


class IdleBreathingMotion:
    """Continuous idle pose matching Pollen's conversation-app breathing move."""

    def __init__(
        self,
        start_head: Any,
        start_antennas: list[float],
        *,
        base_head: Any | None = None,
        interpolation_duration: float = 1.0,
    ) -> None:
        self.start_head = np.asarray(start_head, dtype=np.float64)
        self.start_antennas = np.asarray(start_antennas, dtype=np.float64)
        self.interpolation_duration = interpolation_duration
        self.base_head = np.asarray(
            base_head
            if base_head is not None
            else create_head_pose(0, 0, 0, 0, 0, 0, degrees=True),
            dtype=np.float64,
        )
        self.neutral_antennas = np.array([-0.1745, 0.1745], dtype=np.float64)

    def evaluate(self, elapsed: float) -> tuple[Any, np.ndarray, None]:
        if elapsed < self.interpolation_duration:
            progress = max(0.0, elapsed / self.interpolation_duration)
            head = linear_pose_interpolation(self.start_head, self.base_head, progress)
            antennas = (
                (1.0 - progress) * self.start_antennas
                + progress * self.neutral_antennas
            )
            return head, antennas, None

        breathing_time = elapsed - self.interpolation_duration
        z_offset = 0.005 * np.sin(2.0 * np.pi * 0.1 * breathing_time)
        offset = create_head_pose(
            z=z_offset,
            degrees=True,
            mm=False,
        )
        head = self.base_head @ offset
        antenna_sway = np.deg2rad(15.0) * np.sin(
            2.0 * np.pi * 0.5 * breathing_time
        )
        antennas = np.array([antenna_sway, -antenna_sway], dtype=np.float64)
        return head, antennas, None


class ListeningNodMotion:
    """One short nod when local VAD starts hearing the user."""

    def __init__(
        self,
        start_head: Any,
        *,
        base_head: Any | None = None,
        interpolation_duration: float = 0.25,
        nod_duration: float = 0.45,
        max_pitch_degrees: float = 4.0,
    ) -> None:
        self.start_head = np.asarray(start_head, dtype=np.float64)
        self.interpolation_duration = interpolation_duration
        self.nod_duration = nod_duration
        self.max_pitch_degrees = max_pitch_degrees
        self.base_head = np.asarray(
            base_head
            if base_head is not None
            else create_head_pose(0, 0, 0, 0, 0, 0, degrees=True),
            dtype=np.float64,
        )

    def evaluate(self, elapsed: float) -> Any:
        if elapsed < self.interpolation_duration:
            progress = max(0.0, elapsed / self.interpolation_duration)
            return linear_pose_interpolation(self.start_head, self.base_head, progress)
        nod_time = max(0.0, elapsed - self.interpolation_duration)
        if nod_time >= self.nod_duration:
            pitch = 0.0
        else:
            progress = nod_time / self.nod_duration
            pitch = self.max_pitch_degrees * np.sin(np.pi * progress)
        return self.base_head @ create_head_pose(pitch=pitch, degrees=True)

    def is_moving(self, elapsed: float) -> bool:
        if elapsed < self.interpolation_duration:
            return True
        nod_time = max(0.0, elapsed - self.interpolation_duration)
        return nod_time < self.nod_duration


class SpeakingMotion:
    """Subtle continuous head and antenna motion while Reachy speaks."""

    def __init__(
        self,
        start_head: Any,
        start_antennas: list[float],
        *,
        base_head: Any | None = None,
        interpolation_duration: float = 0.3,
    ) -> None:
        self.start_head = np.asarray(start_head, dtype=np.float64)
        self.start_antennas = np.asarray(start_antennas, dtype=np.float64)
        self.interpolation_duration = interpolation_duration
        self.base_head = np.asarray(
            base_head
            if base_head is not None
            else create_head_pose(0, 0, 0, 0, 0, 0, degrees=True),
            dtype=np.float64,
        )
        self.neutral_antennas = np.deg2rad([-10.0, 10.0])

    def evaluate(self, elapsed: float) -> tuple[Any, np.ndarray]:
        if elapsed < self.interpolation_duration:
            progress = max(0.0, elapsed / self.interpolation_duration)
            head = linear_pose_interpolation(self.start_head, self.base_head, progress)
            antennas = (
                (1.0 - progress) * self.start_antennas
                + progress * self.neutral_antennas
            )
            return head, antennas

        speaking_time = elapsed - self.interpolation_duration
        pitch = (
            1.8 * np.sin(2.0 * np.pi * 0.55 * speaking_time)
            + 0.6 * np.sin(2.0 * np.pi * 1.1 * speaking_time + 0.4)
        )
        yaw = 2.6 * np.sin(2.0 * np.pi * 0.23 * speaking_time + 0.8)
        roll = 1.2 * np.sin(2.0 * np.pi * 0.31 * speaking_time)
        z_offset = 0.002 * np.sin(2.0 * np.pi * 0.7 * speaking_time)
        offset = create_head_pose(
            z=z_offset,
            roll=roll,
            pitch=pitch,
            yaw=yaw,
            degrees=True,
            mm=False,
        )
        head = self.base_head @ offset
        left = -10.0 + 5.0 * np.sin(2.0 * np.pi * 0.72 * speaking_time + 0.3)
        right = 10.0 - 5.0 * np.sin(2.0 * np.pi * 0.67 * speaking_time + 1.1)
        return head, np.deg2rad([left, right])
