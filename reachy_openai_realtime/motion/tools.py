# ABOUTME: OpenAI Realtime tool definitions for physical robot movement — the
# ABOUTME: JSON schemas sent to the model so it can invoke motion commands.
from __future__ import annotations

from typing import Any

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

RECORDED_MOVE_TOOL_DEFINITIONS: dict[str, list[dict[str, Any]]] = {
    "emotion": [
        {
            "type": "function",
            "name": "play_emotion",
            "description": "収録済みの感情ジェスチャーを再生する。emotionには利用可能なエモーション名を正確に指定する。",
            "parameters": {
                "type": "object",
                "properties": {"emotion": {"type": "string"}},
                "required": ["emotion"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "stop_emotion",
            "description": "再生中のエモーションを停止する。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ],
    "dance": [
        {
            "type": "function",
            "name": "play_dance",
            "description": "収録済みのダンスを再生する。danceには利用可能なダンス名を正確に指定する。",
            "parameters": {
                "type": "object",
                "properties": {"dance": {"type": "string"}},
                "required": ["dance"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "stop_dance",
            "description": "再生中のダンスを停止する。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ],
}


def tool_definitions(*, emotions_available: bool, dances_available: bool) -> list[dict[str, Any]]:
    tools = list(TOOL_DEFINITIONS)
    if emotions_available:
        tools.extend(RECORDED_MOVE_TOOL_DEFINITIONS["emotion"])
    if dances_available:
        tools.extend(RECORDED_MOVE_TOOL_DEFINITIONS["dance"])
    return tools
