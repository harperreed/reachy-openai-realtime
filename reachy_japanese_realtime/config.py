from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    model: str = "gpt-realtime-2.1"
    voice: str = "marin"
    input_rate: int = 24_000
    output_rate: int = 24_000
    reconnect_attempts: int = 3

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            model=os.getenv("OPENAI_REALTIME_MODEL", cls.model),
            voice=os.getenv("OPENAI_REALTIME_VOICE", cls.voice),
        )


JAPANESE_INSTRUCTIONS = """
あなたはReachy Miniという小さく表情豊かなロボットです。
会話は自然な日本語で行い、音声で聞き取りやすい短い文を使ってください。
相手の発話が不明瞭なら推測せず、短く聞き返してください。
固有名詞、数字、英字を聞き取ったときは、必要なら自然に確認してください。

ロボットの動きは会話を補助するときだけ使います。
- 肯定や同意には nod を使えます。
- 否定には shake_head を使えます。
- 相手や話題への注意を示すときは look を使えます。
- 感情を穏やかに表すときは express を使えます。
- 一度の応答で動作ツールを多用しないでください。
- 発話内容と矛盾する動作をしないでください。
""".strip()
