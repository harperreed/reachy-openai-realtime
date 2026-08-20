# ABOUTME: Motion arbitration manager — validates, schedules, and executes robot
# ABOUTME: motion commands with priority preemption and ambient generators in a worker thread.
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Literal, Protocol

import numpy as np
from reachy_mini.utils import create_head_pose

from . import tools
from .builtin import IdleBreathingMotion, ListeningNodMotion, SpeakingMotion
from .recorded_moves import RecordedMoveCatalog

logger = logging.getLogger(__name__)

RECORDED_MOVE_TICK_HZ = 50.0  # own playback loop; bounded WS rate, above the 30 Hz ambient tick

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


class MotionPriority(IntEnum):
    """Spec §12 arbitration table. Higher preempts lower; 50 is reserved for look_at_speaker (Phase 3)."""

    STOP = 100
    BARGE_IN = 90
    GESTURE = 75
    EMOTION = 70
    DANCE = 65
    LOOK = 45
    SPEAKING = 20
    LISTENING = 15
    IDLE = 10


@dataclass
class _Activity:
    name: str
    priority: int
    run: Callable[[], None]
    kind: str = "motion"  # "motion" | "emotion" | "dance" — selects the §18 event family


class MotionManager:
    def __init__(
        self,
        robot: ReachyMotionAPI,
        *,
        emotions: RecordedMoveCatalog | None = None,
        dances: RecordedMoveCatalog | None = None,
    ) -> None:
        self.robot = robot
        self._emotions = emotions
        self._dances = dances
        self._slot_lock = threading.Lock()
        self._slot_cv = threading.Condition(self._slot_lock)
        self._pending: _Activity | None = None
        self._current: _Activity | None = None
        self._record: Callable[..., None] | None = None
        self._heartbeat: Callable[[], None] | None = None
        self._cancel_reason: str = "stop"
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

    def attach_recorder(self, record: Callable[..., None]) -> None:
        self._record = record

    def set_heartbeat(self, callback: Callable[[], None]) -> None:
        self._heartbeat = callback

    def _beat(self) -> None:
        if self._heartbeat is None:
            return
        try:
            self._heartbeat()
        except Exception:
            logger.debug("motion heartbeat callback failed", exc_info=True)

    def _emit(self, event: str, **fields: Any) -> None:
        if self._record is None:
            return
        try:
            self._record(event, **fields)
        except Exception:
            logger.debug("motion event emission failed", exc_info=True)

    def _start_activity(
        self, name: str, priority: int, run: Callable[[], None], kind: str = "motion"
    ) -> dict[str, Any]:
        # Check-and-set under ONE lock hold — a split check/act races the worker clearing _current.
        with self._slot_cv:
            blocking = max(
                (a for a in (self._current, self._pending) if a is not None),
                key=lambda a: a.priority,
                default=None,
            )
            if blocking is not None and priority < blocking.priority:
                return {"ok": False, "error": f"busy: {blocking.name} is active at priority {blocking.priority}"}
            if self._current is not None:
                self._cancel_reason = "preempted"
                self._cancel_event.set()
            self._pending = _Activity(name, priority, run, kind)
            self._slot_cv.notify()
        return {"ok": True, "motion": name}

    def submit(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        command = self.validate(name, arguments)
        if name == "stop_motion":
            self.stop_current(reason="stop")
            return {"ok": True, "motion": "stop_motion"}
        if name == "play_emotion":
            return self._submit_recorded("emotion", self._emotions, command.arguments["emotion"])
        if name == "play_dance":
            return self._submit_recorded("dance", self._dances, command.arguments["dance"])
        if name == "stop_emotion":
            return self._stop_recorded("emotion")
        if name == "stop_dance":
            return self._stop_recorded("dance")
        if name == "look":
            result = self._start_activity("look", MotionPriority.LOOK, lambda: self._execute(command))
            if result["ok"]:
                result["arguments"] = command.arguments
            return result
        # nod / shake_head / express
        result = self._start_activity(name, MotionPriority.GESTURE, lambda: self._execute(command))
        if result["ok"]:
            result["arguments"] = command.arguments
        return result

    def play_recorded(self, kind: str, name: str, move: Any) -> dict[str, Any]:
        if kind not in {"emotion", "dance"}:
            raise ValueError(f"unknown recorded-move kind: {kind}")
        priority = MotionPriority.EMOTION if kind == "emotion" else MotionPriority.DANCE
        result = self._start_activity(name, priority, lambda: self._run_recorded(move), kind=kind)
        if result.get("ok"):
            result["motion"] = f"play_{kind}"
            result[kind] = name
            result["duration_s"] = round(float(move.duration), 1)
        return result

    def _submit_recorded(self, kind: str, catalog: RecordedMoveCatalog | None, name: str) -> dict[str, Any]:
        if catalog is None or not catalog.available:
            return {"ok": False, "error": f"{kind} catalog unavailable"}
        move = catalog.get(name)  # ValueError for unknown names propagates to realtime's handler
        return self.play_recorded(kind, name, move)

    def _stop_recorded(self, kind: str) -> dict[str, Any]:
        with self._slot_lock:
            active = self._current or self._pending
            matches = active is not None and active.kind == kind
        if matches:
            # cancel the foreground move only; background enables stay untouched
            with self._slot_cv:
                self._pending = None
                self._cancel_reason = "stop"
                self._cancel_event.set()
        return {"ok": True, "motion": f"stop_{kind}", "stopped": matches}

    def emotion_names(self) -> list[str]:
        return self._emotions.names() if self._emotions is not None else []

    def dance_names(self) -> list[str]:
        return self._dances.names() if self._dances is not None else []

    def tool_definitions(self) -> list[dict[str, Any]]:
        return tools.tool_definitions(
            emotions=self.emotion_names(),
            dances=self.dance_names(),
        )

    def _run_recorded(self, move: Any) -> None:
        tick = 1.0 / RECORDED_MOVE_TICK_HZ
        try:
            head, antennas, body_yaw = move.evaluate(0.0)
        except Exception:
            logger.exception("Recorded move failed to evaluate its first frame")
            return
        try:
            self.robot.goto_target(head=head, antennas=antennas, duration=0.4, body_yaw=body_yaw)
        except Exception:
            logger.debug("Initial goto for recorded move failed", exc_info=True)
        started = time.monotonic()
        duration = float(move.duration)
        while not self._cancel_event.is_set() and not self._stop_event.is_set():
            elapsed = time.monotonic() - started
            if elapsed >= duration:
                break
            t = min(elapsed, duration - 1e-2)  # SDK evaluate() raises at/after the last timestamp
            try:
                head, antennas, body_yaw = move.evaluate(t)
            except Exception:
                logger.exception("Recorded move evaluation failed mid-play")
                return
            try:
                self.robot.set_target(head=head, antennas=antennas, body_yaw=body_yaw)
            except Exception:
                logger.debug("Recorded move set_target failed", exc_info=True)
            time.sleep(tick)

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
        if name == "play_emotion":
            emotion = arguments.get("emotion")
            if not isinstance(emotion, str):
                raise ValueError("play_emotion requires a string 'emotion' argument")
            return MotionCommand(name, {"emotion": emotion})
        if name == "play_dance":
            dance = arguments.get("dance")
            if not isinstance(dance, str):
                raise ValueError("play_dance requires a string 'dance' argument")
            return MotionCommand(name, {"dance": dance})
        if name == "stop_emotion":
            return MotionCommand(name, {})
        if name == "stop_dance":
            return MotionCommand(name, {})
        raise ValueError(f"unknown motion tool: {name}")

    def stop_current(self, reason: str = "stop") -> None:
        self.set_idle_enabled(False)
        self._listening_enabled.clear()
        self._speaking_enabled.clear()
        with self._slot_cv:
            self._cancel_reason = reason
            self._cancel_event.set()
            self._pending = None
            self._slot_cv.notify()
        # ReachyMini.cancel_move() also calls media.stop_playing(). On Wireless,
        # capture and playback share one GStreamer pipeline, so that method also
        # stops the microphone. The local cancel flag and pending slot clear are the
        # safe way to stop motions owned by this controller.

    def close(self) -> None:
        self.set_idle_enabled(False)
        self._stop_event.set()
        self.stop_current(reason="shutdown")
        with self._slot_cv:
            self._slot_cv.notify()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._goto_absolute(0, 0, [0, 0], 0.7)

    def _has_foreground(self) -> bool:
        with self._slot_lock:
            return self._current is not None or self._pending is not None

    def _reset_ambient_generators(self) -> None:
        self._idle_motion = None
        self._idle_started_at = None
        self._listening_motion = None
        self._listening_started_at = None
        self._listening_was_moving = None
        self._speaking_motion = None
        self._speaking_started_at = None

    def _return_to_base(self) -> None:
        try:
            self.robot.goto_target(
                head=self._get_base_head(),
                antennas=np.deg2rad([-10.0, 10.0]),
                duration=0.4,
                body_yaw=None,
            )
        except Exception:
            logger.debug("Could not return to base head", exc_info=True)

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
            with self._slot_cv:
                if self._pending is None:
                    self._slot_cv.wait(timeout=self._idle_period)
                activity, self._pending = self._pending, None
                if activity is not None:
                    self._current = activity
                    # Clear inside the lock so a concurrent _start_activity cannot
                    # set the cancel event between us committing _current and here.
                    self._cancel_event.clear()
            self._beat()
            if activity is None:
                if not self._has_foreground():
                    self._update_ambient_motion()
                continue
            self._reset_ambient_generators()
            self._cancel_reason = "stop"
            started_at = time.monotonic()
            family = activity.kind  # "motion" | "emotion" | "dance"
            label_key = {"motion": "motion", "emotion": "emotion", "dance": "dance"}[family]
            self._emit(f"{family}.started", **{label_key: activity.name, "priority": activity.priority})
            try:
                activity.run()
            except Exception:
                logger.exception("Motion failed: %s", activity.name)
            duration_ms = round((time.monotonic() - started_at) * 1000.0, 1)
            if self._cancel_event.is_set():
                self._emit("motion.cancelled", motion=activity.name, reason=self._cancel_reason)
            else:
                self._emit(f"{family}.completed", **{label_key: activity.name, "duration_ms": duration_ms})
            with self._slot_lock:
                self._current = None
                has_pending = self._pending is not None
            if not has_pending:
                self._return_to_base()  # a preempting activity is about to move anyway — skip the detour
            self._last_activity_at = time.monotonic()
            self._beat()

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
