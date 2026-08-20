# ABOUTME: OpenAI Realtime tool definitions for physical robot movement — the
# ABOUTME: JSON schemas sent to the model so it can invoke motion commands.
from __future__ import annotations

import copy
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
        "description": "頭とアンテナの小さなプリセット動作で、会話中にさりげなく感情のニュアンスを添える。",
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
            "description": "収録済みの表情豊かな感情ジェスチャー（数秒間の全身パフォーマンス）を再生する。感情を見せてと言われたらexpressよりこちらを優先する。",
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
            "description": "収録済みのダンス（数秒間の全身パフォーマンス）を再生する。踊ってと言われたらこれを使う。",
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


def _recorded_entries(kind: str, play_name: str, param: str, names: list[str]) -> list[dict[str, Any]]:
    entries = copy.deepcopy(RECORDED_MOVE_TOOL_DEFINITIONS[kind])
    for entry in entries:
        if entry["name"] == play_name:
            entry["parameters"]["properties"][param]["enum"] = list(names)
    return entries


def tool_definitions(*, emotions: list[str], dances: list[str]) -> list[dict[str, Any]]:
    tools = list(TOOL_DEFINITIONS)
    if emotions:
        tools.extend(_recorded_entries("emotion", "play_emotion", "emotion", emotions))
    if dances:
        tools.extend(_recorded_entries("dance", "play_dance", "dance", dances))
    return tools
