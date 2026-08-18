# Gotchas

- **Reachy Mini Wireless shares ONE GStreamer pipeline for mic and speaker.**
  `media.stop_playing()` and `ReachyMini.cancel_move()` stall the microphone. After
  `audio.clear_player()`, always re-assert `media.start_recording()` (see `_clear_playback` in
  `reachy_openai_realtime/realtime.py` and `stop_current` in `motion.py`).
- **`media.get_audio_sample()` must be drained continuously** — it returns everything buffered
  since the last call, and the SDK-side buffer grows without bound otherwise (reachy_mini
  issue #436). Never gate the drain on conversation state; gate downstream consumption.
- **OpenAI Realtime sessions hard-cap at 60 minutes.** Server-side closes are routine, not
  failures. Never treat a clean close as fatal.
- **`gpt-realtime-2.1` is the current model** — bare `gpt-realtime` / `gpt-realtime-mini` are
  deprecated (shutdown 2027-01-20). Verify model names against
  https://developers.openai.com/api/docs/models before calling one fake.
- **The Reachy app entry-point group is `reachy_mini_apps`, and the daemon does NOT
  auto-restart crashed apps.** The outer loop in `main.py:run()` is the only recovery path.
  App stop = SIGINT, then SIGKILL after ~20 s — worker threads need bounded joins.
- **Python floor is 3.10**: no `asyncio.timeout()`, no `StrEnum`. Ruff line length is 110.
- Production-hardening spec: `docs/production-hardening-spec.md`. Phase 1 plan:
  `docs/superpowers/plans/2026-08-17-phase1-reliability-foundation.md`.
- Mic drain lives in `reachy_openai_realtime/audio/capture.py` (`CaptureWorker`). The stall
  ladder escalates restart_capture → restart_media → restart_session and NEVER reboots the OS.
- Playback freshness: `audio/playback.py` drops oldest past 500 ms and cancels + relistens at
  1 s (`audio.playback.overrun` in events.jsonl). Don't "fix" audio gaps by buffering more.
- Fatal vs transient connection errors: `session/recovery.py:classify_connection_error` —
  429 is TRANSIENT and is checked BEFORE the 4xx→FATAL rule. Keep that ordering.
- Reconnect policy: infinite jittered backoff 1→30 s, reset after 60 s healthy; fatal config
  errors park in `config_error` until settings change (`main.py` fingerprint wait loop).
- **"Phase 2" names two different things.** Hardening-spec §29 Phase 2 = motion (issues #13–15).
  The 2026-08-18 features spec ("Reachy Phase 2 — Remaining Feature Specs") = idle/sleep, external
  brain, memory, push events (issues #16–20; the verbatim spec lives in those issues' details
  blocks). Say which one you mean. GitHub MILESTONES are the tracker's real ordering: Phase 1.5
  (cleanups & self-monitoring, #1–11) → Phase 2 (presence + motion chains) → Phase 3 (ToolExecutor,
  brain, memory, push, #18–21). Issue titles cite "hardening spec §N" or "features spec Part 2X";
  #16 is the pinned epic with #17–20 as sub-issues. File new issues into a milestone with those
  title conventions.
- **`uv run <tool>` silently falls back to PATH (mise-global) when the tool isn't in a synced
  group.** Dev tools (ruff, pytest) live in `[dependency-groups] dev` — never only in an extra.
  A "clean" check may be a different version than the lock pins; `uv run which <tool>` must
  point into `.venv`. Canonical check: `uv run ruff check . && uv run pytest`.
- **The robot's dashboard "daemon restart" does NOT restart the daemon process.** `POST
  /api/daemon/stop|start` recycles the hardware-daemon object inside the same Python process;
  the FastAPI/AppManager singleton and its in-memory `current_app` survive. If `stop_current_app`
  wedges mid-cleanup (reachy_mini 1.9.0 can park an app in state `stopping` forever — cleanup
  after "App stopped successfully" never reaches `current_app = None`), every start AND stop
  returns 400 ("An app is already running" / "No app is currently running") no matter how many
  dashboard restarts, reinstalls, or cache clears you do. Recovery: `ssh pollen@<robot>` then
  `sudo systemctl restart reachy-mini-daemon.service` (service restart, never an OS reboot).
  Diagnose with `curl localhost:8000/api/apps/current-app-status` on the robot; `state:
  "stopping"` with no app process = the wedge. `uvx` lives at `/opt/uv` on Reachy OS if you
  need py-spy.
