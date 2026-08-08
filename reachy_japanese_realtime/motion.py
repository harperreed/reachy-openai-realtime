from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np
from reachy_mini.utils import create_head_pose

logger = logging.getLogger(__name__)

Direction = Literal["front", "left", "right", "up", "down"]
Emotion = Literal["neutral", "happy", "curious", "surprised", "sad"]


class ReachyMotionAPI(Protocol):
    def goto_target(
        self,
        head: Any = None,
        antennas: Any = None,
        duration: float = 0.5,
        body_yaw: float | None = 0.0,
    ) -> None: ...

    def cancel_move(self) -> None: ...


@dataclass(frozen=True)
class MotionCommand:
    name: str
    arguments: dict[str, Any]


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "look",
        "description": "顔を安全なプリセット方向へ向ける。会話上必要なときだけ使う。",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["front", "left", "right", "up", "down"],
                }
            },
            "required": ["direction"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "nod",
        "description": "肯定や同意を示すため、穏やかにうなずく。",
        "parameters": {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["count"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "shake_head",
        "description": "否定を示すため、穏やかに首を横へ振る。",
        "parameters": {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["count"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "express",
        "description": "頭とアンテナの安全なプリセットで感情を表現する。",
        "parameters": {
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "enum": ["neutral", "happy", "curious", "surprised", "sad"],
                }
            },
            "required": ["emotion"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "stop_motion",
        "description": "実行中および待機中のロボット動作を停止する。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class MotionController:
    def __init__(self, robot: ReachyMotionAPI) -> None:
        self.robot = robot
        self._queue: queue.Queue[MotionCommand | None] = queue.Queue(maxsize=8)
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="motion-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        command = self.validate(name, arguments)
        if name == "stop_motion":
            self.stop_current()
            return {"ok": True, "motion": name}
        try:
            self._queue.put_nowait(command)
        except queue.Full:
            return {"ok": False, "error": "motion queue is full"}
        return {"ok": True, "motion": name, "arguments": command.arguments}

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
        self._cancel_event.set()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        try:
            self.robot.cancel_move()
        except Exception:
            logger.debug("cancel_move failed", exc_info=True)

    def close(self) -> None:
        self._stop_event.set()
        self.stop_current()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=2.0)
        self._goto(0, 0, [0, 0], 0.7)

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            command = self._queue.get()
            if command is None:
                self._queue.task_done()
                return
            self._cancel_event.clear()
            try:
                self._execute(command)
            except Exception:
                logger.exception("Motion failed: %s", command.name)
            finally:
                self._queue.task_done()

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
            self._goto(pitch, yaw, [0, 0], 0.55)
        elif command.name == "nod":
            for _ in range(command.arguments["count"]):
                if self._cancel_event.is_set():
                    break
                self._goto(13, 0, None, 0.22)
                self._goto(-5, 0, None, 0.22)
            self._goto(0, 0, None, 0.3)
        elif command.name == "shake_head":
            for _ in range(command.arguments["count"]):
                if self._cancel_event.is_set():
                    break
                self._goto(0, 15, None, 0.22)
                self._goto(0, -15, None, 0.22)
            self._goto(0, 0, None, 0.3)
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
            self._goto(pitch, yaw, antennas, 0.55)

    def _goto(
        self,
        pitch: float,
        yaw: float,
        antennas_deg: list[float] | None,
        duration: float,
    ) -> None:
        if self._cancel_event.is_set() and not self._stop_event.is_set():
            return
        antennas = np.deg2rad(antennas_deg) if antennas_deg is not None else None
        self.robot.goto_target(
            head=create_head_pose(pitch=pitch, yaw=yaw, degrees=True),
            antennas=antennas,
            duration=duration,
            body_yaw=None,
        )

