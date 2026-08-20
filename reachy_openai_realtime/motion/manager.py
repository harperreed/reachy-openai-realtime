# ABOUTME: Motion arbitration manager — validates, queues, and executes robot
# ABOUTME: motion commands while running ambient generators in a worker thread.
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np
from reachy_mini.utils import create_head_pose

from .builtin import IdleBreathingMotion, ListeningNodMotion, SpeakingMotion

logger = logging.getLogger(__name__)

Direction = Literal["front", "left", "right", "up", "down"]
Emotion = Literal["neutral", "happy", "curious", "surprised", "sad"]


class ReachyMotionAPI(Protocol):
    def set_target(
        self,
        head: Any = None,
        antennas: Any = None,
        body_yaw: float | None = None,
    ) -> None: ...

    def goto_target(
        self,
        head: Any = None,
        antennas: Any = None,
        duration: float = 0.5,
        body_yaw: float | None = 0.0,
    ) -> None: ...

    def get_current_head_pose(self) -> Any: ...

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]: ...


@dataclass(frozen=True)
class MotionCommand:
    name: str
    arguments: dict[str, Any]


class MotionController:
    def __init__(self, robot: ReachyMotionAPI) -> None:
        self.robot = robot
        self._queue: queue.Queue[MotionCommand | None] = queue.Queue(maxsize=8)
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        self._idle_enabled = threading.Event()
        self._listening_enabled = threading.Event()
        self._speaking_enabled = threading.Event()
        self._idle_motion: IdleBreathingMotion | None = None
        self._idle_started_at: float | None = None
        self._listening_motion: ListeningNodMotion | None = None
        self._listening_started_at: float | None = None
        self._listening_was_moving: bool | None = None
        self._speaking_motion: SpeakingMotion | None = None
        self._speaking_started_at: float | None = None
        self._base_head: np.ndarray | None = None
        self._last_activity_at = time.monotonic()
        self._idle_start_delay = 0.3
        self._idle_period = 1.0 / 30.0
        self._thread = threading.Thread(target=self._worker, name="motion-worker", daemon=True)

    def start(self) -> None:
        self._get_base_head()
        self._thread.start()

    def submit(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.set_listening_enabled(False)
        self.set_speaking_enabled(False)
        self.set_idle_enabled(False)
        command = self.validate(name, arguments)
        if name == "stop_motion":
            self.stop_current()
            return {"ok": True, "motion": name}
        try:
            self._queue.put_nowait(command)
        except queue.Full:
            return {"ok": False, "error": "motion queue is full"}
        return {"ok": True, "motion": name, "arguments": command.arguments}

    def set_listening_enabled(self, enabled: bool) -> None:
        """Run one restrained nod when human speech starts."""
        if enabled:
            if self._listening_enabled.is_set():
                return
            self._speaking_enabled.clear()
            self.stop_current()
            self._listening_enabled.set()
        else:
            self._listening_enabled.clear()

    def set_speaking_enabled(self, enabled: bool) -> None:
        """Animate subtly only while assistant audio is being played."""
        if enabled:
            if self._speaking_enabled.is_set():
                return
            self._idle_enabled.clear()
            self._listening_enabled.clear()
            self._speaking_enabled.set()
        else:
            self._speaking_enabled.clear()

    def set_idle_enabled(self, enabled: bool) -> None:
        """Allow subtle breathing only while the conversation is waiting."""
        if enabled:
            if self._idle_enabled.is_set():
                return
            self._last_activity_at = time.monotonic()
            self._idle_enabled.set()
        else:
            if not self._idle_enabled.is_set():
                return
            self._last_activity_at = time.monotonic()
            self._idle_enabled.clear()

    @staticmethod
    def validate(name: str, arguments: dict[str, Any]) -> MotionCommand:
        if name == "look":
            direction = arguments.get("direction")
            if direction not in {"front", "left", "right", "up", "down"}:
                raise ValueError("invalid look direction")
            return MotionCommand(name, {"direction": direction})
        if name in {"nod", "shake_head"}:
            count = arguments.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError("count must be an integer")
            return MotionCommand(name, {"count": max(1, min(3, count))})
        if name == "express":
            emotion = arguments.get("emotion")
            if emotion not in {"neutral", "happy", "curious", "surprised", "sad"}:
                raise ValueError("invalid emotion")
            return MotionCommand(name, {"emotion": emotion})
        if name == "stop_motion":
            return MotionCommand(name, {})
        raise ValueError(f"unknown motion tool: {name}")

    def stop_current(self) -> None:
        self.set_idle_enabled(False)
        self._listening_enabled.clear()
        self._speaking_enabled.clear()
        self._cancel_event.set()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        # ReachyMini.cancel_move() also calls media.stop_playing(). On Wireless,
        # capture and playback share one GStreamer pipeline, so that method also
        # stops the microphone. The local cancel flag and queue drain are the
        # safe way to stop motions owned by this controller.

    def close(self) -> None:
        self.set_idle_enabled(False)
        self._stop_event.set()
        self.stop_current()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=2.0)
        self._goto_absolute(0, 0, [0, 0], 0.7)

    def _get_base_head(self) -> np.ndarray:
        if self._base_head is None:
            try:
                self._base_head = np.asarray(
                    self.robot.get_current_head_pose(), dtype=np.float64
                ).copy()
            except Exception:
                logger.debug("Could not read initial persistent head pose", exc_info=True)
                self._base_head = np.asarray(
                    create_head_pose(0, 0, 0, 0, 0, 0, degrees=True),
                    dtype=np.float64,
                )
        return self._base_head.copy()

    def _set_base_head(self, head: Any) -> None:
        self._base_head = np.asarray(head, dtype=np.float64).copy()

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                command = self._queue.get(timeout=self._idle_period)
            except queue.Empty:
                self._update_ambient_motion()
                continue
            if command is None:
                self._queue.task_done()
                return
            self._idle_motion = None
            self._idle_started_at = None
            self._listening_motion = None
            self._listening_started_at = None
            self._listening_was_moving = None
            self._speaking_motion = None
            self._speaking_started_at = None
            self._cancel_event.clear()
            try:
                self._execute(command)
            except Exception:
                logger.exception("Motion failed: %s", command.name)
            finally:
                self._last_activity_at = time.monotonic()
                self._queue.task_done()

    def _update_ambient_motion(self) -> None:
        if self._listening_enabled.is_set():
            self._idle_motion = None
            self._idle_started_at = None
            self._speaking_motion = None
            self._speaking_started_at = None
            self._update_listening_motion()
            return
        if self._speaking_enabled.is_set():
            self._idle_motion = None
            self._idle_started_at = None
            self._listening_motion = None
            self._listening_started_at = None
            self._listening_was_moving = None
            self._update_speaking_motion()
            return
        if self._listening_motion is not None:
            self._listening_motion = None
            self._listening_started_at = None
            self._listening_was_moving = None
            try:
                self.robot.goto_target(
                    head=self._get_base_head(),
                    duration=0.2,
                    body_yaw=None,
                )
            except Exception:
                logger.debug("Could not return from listening nod", exc_info=True)
        if self._speaking_motion is not None:
            self._speaking_motion = None
            self._speaking_started_at = None
            try:
                self.robot.goto_target(
                    head=self._get_base_head(),
                    antennas=np.deg2rad([-10.0, 10.0]),
                    duration=0.25,
                    body_yaw=None,
                )
            except Exception:
                logger.debug("Could not return from speaking motion", exc_info=True)
        self._update_idle_motion()

    def _update_speaking_motion(self) -> None:
        now = time.monotonic()
        if self._speaking_motion is None or self._speaking_started_at is None:
            try:
                start_head = self.robot.get_current_head_pose()
                _, start_antennas = self.robot.get_current_joint_positions()
            except Exception:
                logger.debug("Could not read pose for speaking motion", exc_info=True)
                return
            self._speaking_motion = SpeakingMotion(
                start_head,
                start_antennas,
                base_head=self._get_base_head(),
            )
            self._speaking_started_at = now
        try:
            head, antennas = self._speaking_motion.evaluate(now - self._speaking_started_at)
            self.robot.set_target(
                head=head,
                antennas=antennas,
                body_yaw=None,
            )
        except Exception:
            logger.debug("Speaking motion command failed", exc_info=True)
            self._speaking_motion = None
            self._speaking_started_at = None

    def _update_listening_motion(self) -> None:
        now = time.monotonic()
        if self._listening_motion is None or self._listening_started_at is None:
            try:
                start_head = self.robot.get_current_head_pose()
            except Exception:
                logger.debug("Could not read pose for listening nod", exc_info=True)
                return
            self._listening_motion = ListeningNodMotion(
                start_head,
                base_head=self._get_base_head(),
            )
            self._listening_started_at = now
            self._listening_was_moving = None
        try:
            elapsed = now - self._listening_started_at
            moving = self._listening_motion.is_moving(elapsed)
            if not moving and self._listening_was_moving is False:
                return
            self.robot.set_target(
                head=self._listening_motion.evaluate(elapsed),
                body_yaw=None,
            )
            self._listening_was_moving = moving
        except Exception:
            logger.debug("Listening nod command failed", exc_info=True)
            self._listening_motion = None
            self._listening_started_at = None
            self._listening_was_moving = None

    def _update_idle_motion(self) -> None:
        if not self._idle_enabled.is_set():
            self._idle_motion = None
            self._idle_started_at = None
            return
        now = time.monotonic()
        if now - self._last_activity_at < self._idle_start_delay:
            return
        if self._idle_motion is None or self._idle_started_at is None:
            try:
                start_head = self.robot.get_current_head_pose()
                _, start_antennas = self.robot.get_current_joint_positions()
            except Exception:
                logger.debug("Could not read pose for idle breathing", exc_info=True)
                self._last_activity_at = now
                return
            self._idle_motion = IdleBreathingMotion(
                start_head,
                start_antennas,
                base_head=self._get_base_head(),
            )
            self._idle_started_at = now

        try:
            head, antennas, body_yaw = self._idle_motion.evaluate(
                now - self._idle_started_at
            )
            self.robot.set_target(
                head=head,
                antennas=antennas,
                body_yaw=body_yaw,
            )
        except Exception:
            logger.debug("Idle breathing command failed", exc_info=True)
            self._idle_motion = None
            self._idle_started_at = None
            self._last_activity_at = now

    def _execute(self, command: MotionCommand) -> None:
        if command.name == "look":
            poses = {
                "front": (0, 0),
                "left": (0, 22),
                "right": (0, -22),
                "up": (-14, 0),
                "down": (14, 0),
            }
            pitch, yaw = poses[command.arguments["direction"]]
            target_head = create_head_pose(pitch=pitch, yaw=yaw, degrees=True)
            self._goto_head(target_head, [0, 0], 0.55)
            self._set_base_head(target_head)
        elif command.name == "nod":
            for _ in range(command.arguments["count"]):
                if self._cancel_event.is_set():
                    break
                self._goto_relative(13, 0, None, 0.22)
                self._goto_relative(-5, 0, None, 0.22)
            self._goto_relative(0, 0, None, 0.3)
        elif command.name == "shake_head":
            for _ in range(command.arguments["count"]):
                if self._cancel_event.is_set():
                    break
                self._goto_relative(0, 15, None, 0.22)
                self._goto_relative(0, -15, None, 0.22)
            self._goto_relative(0, 0, None, 0.3)
        elif command.name == "express":
            emotion = command.arguments["emotion"]
            presets = {
                "neutral": (0, 0, [0, 0]),
                "happy": (-6, 4, [28, -28]),
                "curious": (-3, 12, [18, 5]),
                "surprised": (-12, 0, [38, -38]),
                "sad": (10, 0, [-18, 18]),
            }
            pitch, yaw, antennas = presets[emotion]
            self._goto_relative(pitch, yaw, antennas, 0.55)

    def _goto_absolute(
        self,
        pitch: float,
        yaw: float,
        antennas_deg: list[float] | None,
        duration: float,
    ) -> None:
        if self._cancel_event.is_set() and not self._stop_event.is_set():
            return
        self._goto_head(
            create_head_pose(pitch=pitch, yaw=yaw, degrees=True),
            antennas_deg,
            duration,
        )

    def _goto_relative(
        self,
        pitch: float,
        yaw: float,
        antennas_deg: list[float] | None,
        duration: float,
    ) -> None:
        if self._cancel_event.is_set() and not self._stop_event.is_set():
            return
        offset = create_head_pose(pitch=pitch, yaw=yaw, degrees=True)
        self._goto_head(self._get_base_head() @ offset, antennas_deg, duration)

    def _goto_head(
        self,
        head: Any,
        antennas_deg: list[float] | None,
        duration: float,
    ) -> None:
        antennas = np.deg2rad(antennas_deg) if antennas_deg is not None else None
        self.robot.goto_target(
            head=head,
            antennas=antennas,
            duration=duration,
            body_yaw=None,
        )
