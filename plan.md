# Reachy Mini OpenAI Realtime — implementation notes

## Goal

Provide low-latency, multilingual speech-to-speech conversation on Reachy Mini Wireless with safe motion tools, optional turn-start camera input, and observable runtime diagnostics.

## Current decisions

- Runtime: Reachy Mini Wireless
- Realtime model: `gpt-realtime-2.1`
- Language: GUI-selectable, English by default
- Authentication: robot-local `OPENAI_API_KEY`, never embedded in source or logs
- Audio turns: local far-field VAD with manual Realtime buffer commit
- Camera: OFF by default; one image at detected speech start when enabled
- Motion: bounded semantic presets executed through a dedicated controller

## Safety boundaries

- The model cannot submit raw joint angles.
- Tool arguments are validated and bounded by the app.
- User speech cancels queued assistant audio and active conversational motion.
- Listening motion is a single short nod to avoid motor noise interfering with VAD.
- Wireless recording is not stopped when motion is cancelled.
