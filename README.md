---
title: Reachy OpenAI Realtime
emoji: 🤖
colorFrom: red
colorTo: blue
sdk: static
pinned: false
short_description: Multilingual Realtime voice and motion for Reachy Mini
tags:
 - reachy_mini
 - reachy_mini_python_app
---

# Reachy Mini OpenAI Realtime

An open-source, multilingual voice, vision, and motion app for Reachy Mini Wireless.
It connects the robot directly to OpenAI `gpt-realtime-2.1` for speech-to-speech conversation—without chaining separate speech recognition and text-to-speech services.

[GitHub source](https://github.com/tinjyuu/reachy-openai-realtime) · [Hugging Face Space](https://huggingface.co/spaces/tinjyuu/reachy_openai_realtime)

> This is an independent community project and is not affiliated with or endorsed by OpenAI or Pollen Robotics.

## Features

- Speech-to-speech conversations in nine selectable languages
- English by default, with the entire UI and runtime log following the selected language
- Local far-field voice activity detection tuned for the Wireless ReSpeaker
- Barge-in: speaking while Reachy responds cancels queued audio and truncates conversation audio correctly
- Safe semantic motion tools: `look`, `nod`, `shake_head`, `express`, and `stop_motion`
- Gentle idle motion, one short listening nod, and subtle head/antenna motion while Reachy speaks
- Optional camera input, disabled by default, sending one still image when speech starts
- Live connection, microphone, conversation, motion, camera, and Realtime event diagnostics
- API key storage outside the package with restrictive filesystem permissions

## Supported languages

English is the default. The management UI can switch the next response to:

- English
- 日本語
- 中文
- 한국어
- Español
- Français
- Deutsch
- Italiano
- Português

The language selection is persisted on the robot. It changes the full management UI immediately—including status details and activity logs—and changes the spoken conversation from the next response. The app supplies the selected language through Realtime session and response instructions, following OpenAI's documented session configuration flow.

## Install on Reachy Mini Wireless

See [`docs/WIRELESS.md`](docs/WIRELESS.md) for the full installation and hardware checklist.

After installation, open the app from the Reachy Mini dashboard and enter an `OPENAI_API_KEY` in the settings UI. Once Reachy says “Hello. Talk to me.”, the conversation is ready.

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | required | OpenAI API authentication |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | Realtime model override |
| `OPENAI_REALTIME_VOICE` | `marin` | Realtime output voice |
| `REACHY_OPENAI_REALTIME_LANGUAGE` | `en` | Initial conversation language |

`marin` is one of the voices OpenAI recommends for best Realtime audio quality.

## API key security

The settings UI stores the key in:

```text
~/.config/reachy-mini/apps/reachy_openai_realtime/.env
```

The directory is mode `0700`, the file is mode `0600`, and no settings or diagnostics API returns the saved value. Existing installations are migrated from the former `reachy_japanese_realtime` configuration directory without placing the key in the package.

Never put an API key in source code, commits, issues, screenshots, or a Hugging Face Space. Before publishing, run:

```bash
uv run python scripts/check_secrets.py
```

The same scan runs in GitHub Actions together with tests and linting.

## Camera behavior and cost

The AI camera starts OFF. When enabled, the UI shows a local preview and the app sends one JPEG at the beginning of each detected user turn using a Realtime `conversation.item.create` item with `input_image`. Image inputs are billable.

## Development

```bash
uv sync --extra dev
uv run python scripts/check_secrets.py
uv run pytest -q
uv run ruff check .
uv run reachy-mini-app-assistant check .
```

The motion layer validates tool names and arguments and maps them to bounded presets. The model never receives raw joint-angle control.

## License

[MIT](LICENSE)
