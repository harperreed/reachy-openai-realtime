---
title: Reachy Japanese Realtime
emoji: 👋
colorFrom: red
colorTo: blue
sdk: static
pinned: false
short_description: Japanese-first OpenAI Realtime voice and motion app for Reachy Mini Wireless
tags:
 - reachy_mini
 - reachy_mini_python_app
---

# Reachy Japanese Realtime

Reachy Mini Wireless向けの、日本語音声対話と安全なモーション制御を組み合わせたアプリです。
OpenAI `gpt-realtime-2.1` を使い、音声を直接理解・生成します。音声認識と読み上げを別々に連結する方式ではありません。

## Features

- 日本語のspeech-to-speech会話
- Semantic VADによる自然な発話終了判定と割り込み
- `look`、`nod`、`shake_head`、`express`、`stop_motion` のツール呼び出し
- モデルに生の関節角度を公開しない安全なモーションプリセット
- API音声24kHzとReachy Mini Wireless音声16kHzの軽量変換
- カメラを使わないCM4向け軽量構成

## Local verification

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run reachy-mini-app-assistant check .
```

## Run on Reachy Mini Wireless

詳細は [`docs/WIRELESS.md`](docs/WIRELESS.md) を参照してください。`OPENAI_API_KEY` は必須です。

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | required | OpenAI API authentication |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | Realtime model override |
| `OPENAI_REALTIME_VOICE` | `marin` | Realtime output voice |

`marin` はOpenAIが高品質用途に推奨するRealtime voiceの一つです。

## Safety

モデルのfunction callは意味レベルの命令として検証され、専用キューからReachy Mini SDKへ送られます。
未定義ツール、不正な方向・感情、範囲外の回数は拒否または制限されます。ユーザーが話し始めると、未再生音声と待機中の動作を停止します。
