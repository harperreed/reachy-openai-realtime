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
- Safe semantic motion tools: `look`, `nod`, `shake_head`, `express`, `play_emotion`, `stop_emotion`, `play_dance`, `stop_dance`, and `stop_motion`; ambient motions preserve the selected look direction
- Gentle idle motion, one short listening nod, and subtle head/antenna motion while Reachy speaks
- Optional camera input, disabled by default, sending one still image when speech starts
- Persistent cumulative input/output token totals and a USD estimate from `response.done` usage
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
uv sync
uv run python scripts/check_secrets.py
uv run pytest -q
uv run ruff check .
uv run reachy-mini-app-assistant check .
```

The motion layer validates tool names and arguments and maps them to bounded presets. The model never receives raw joint-angle control.

### Motion tools

| Tool | What it does |
|---|---|
| `look` | Turns Reachy's head toward a direction preset |
| `nod` | Short vertical nod |
| `shake_head` | Short horizontal shake |
| `express` | Antenna and head expression from a built-in set |
| `play_emotion` | Plays a named recorded move from [`pollen-robotics/reachy-mini-emotions-library`](https://huggingface.co/datasets/pollen-robotics/reachy-mini-emotions-library); validated against the live catalog on startup |
| `stop_emotion` | Cancels any running emotion move |
| `play_dance` | Plays a named recorded move from [`pollen-robotics/reachy-mini-dances-library`](https://huggingface.co/datasets/pollen-robotics/reachy-mini-dances-library); validated against the live catalog on startup |
| `stop_dance` | Cancels any running dance move |
| `stop_motion` | Stops all motion immediately; always wins regardless of what is running |

Catalog tools (`play_emotion`, `play_dance`) are advertised to the model only when their respective catalog is ready. If a catalog is unavailable the tools are absent from the session and voice conversation continues without them (§25).

### Motion priority (§12)

Multiple motion requests are arbitrated by priority. An incoming request preempts the current motion if and only if its priority is equal to or higher than the running one. Lower-priority requests are rejected with a "busy" reply the model can react to.

| Priority | Name | Examples |
|---|---|---|
| 100 | STOP | `stop_motion` |
| 90 | BARGE_IN | user interruption |
| 75 | GESTURE | `nod`, `shake_head`, `express` |
| 70 | EMOTION | `play_emotion` |
| 65 | DANCE | `play_dance` |
| 45 | LOOK | `look` |
| 20 | SPEAKING | head/antenna motion while speaking |
| 15 | LISTENING | listening nod |
| 10 | IDLE | breathing idle |

`stop_motion` (priority 100) always preempts everything.

The usage panel starts tracking after this feature is installed. It stores token counters only in the robot's private app configuration directory; it does not store conversation audio or transcripts. The USD amount is an estimate based on the published `gpt-realtime-2.1` rates and should be checked against the OpenAI billing dashboard for the final amount.

## Reliability & recovery

The app self-heals from network drops, API timeouts, and audio stalls without manual restarts.

### Session state machine

Every connection runs through an explicit FSM (`reachy_openai_realtime/session/fsm.py`). States:

`DISCONNECTED` → `CONNECTING` → `INITIALIZING` → `LISTENING` ↔ `USER_SPEAKING` → `WAITING_RESPONSE` → `ASSISTANT_SPEAKING` ↔ `INTERRUPTING`; `TOOL_EXECUTION` and `RECOVERING` are reachable from any active state; `STOPPING` → `DISCONNECTED` ends the session cleanly.

Every state transition is written to `events.jsonl` as an `fsm.transition` entry.

### Reconnect policy

The app retries connections indefinitely with jittered exponential backoff: delays of 1 → 2 → 4 → 8 → 15 → 30 seconds, each ±20%, then held at 30 s. A session that stays healthy for 60 s resets the counter to zero. OpenAI Realtime hard-caps sessions at 60 minutes, so periodic server-initiated closes are normal — the app treats them as transient and reconnects.

Authentication failures, bad model names, and other config errors (HTTP 4xx, except 429) stop the retry loop and surface an error in the UI. The app waits for a settings change before reconnecting.

### Watchdog deadlines

An expectation-based watchdog (`reachy_openai_realtime/session/watchdog.py`) arms a deadline when a protocol operation starts and disarms it on the expected server reply. A missed deadline tears down the connection for a clean rebuild.

| Operation | Deadline |
|---|---|
| `session_update` | 5 s |
| `response_create` | 5 s |
| `first_output` | 15 s |
| `response_cancel` | 3 s |
| `tool_response` | 5 s |
| `input_append` | 5 s |
| `camera_item` | 5 s |

### Mic recovery ladder

The capture worker (`reachy_openai_realtime/audio/capture.py`) drains `media.get_audio_sample()` continuously regardless of session state. If the mic stalls for 1.75 s, the `AudioRecoveryLadder` escalates in steps (3 s cooldown between each):

1. `restart_capture` — cycle GStreamer recording state (`stop_recording`/`start_recording`; the capture thread keeps running)
2. `restart_media` — restart the GStreamer media pipeline
3. `restart_session` — rebuild the app session via the outer run loop

The robot is never rebooted automatically.

### Playback freshness

The playback buffer (`reachy_openai_realtime/audio/playback.py`) targets 200 ms of queued audio. Above 500 ms it drops the oldest chunks first. Above 1 s it cancels the current response and relistens (`audio.playback.overrun` in `events.jsonl`). Freshness takes priority over completeness.

### Observability

Runtime logs live in:

```text
~/.config/reachy-mini/apps/reachy_openai_realtime/events.jsonl
~/.config/reachy-mini/apps/reachy_openai_realtime/application.log
```

Both files rotate at 2–5 MB and keep two generations. Both files pass through a redaction filter that masks OpenAI-style keys (`sk-…`) before writing — structured event fields in `events.jsonl` and the fully rendered log line (including tracebacks) in `application.log`. Raw microphone audio is never written to either file.

Live metrics (connection uptime, latency percentiles, queue depths, reconnect counts) are available at `/api/status` and the full diagnostics breakdown at `/api/diagnostics`.

## License

[MIT](LICENSE)
