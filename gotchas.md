# Gotchas

- **Reachy Mini Wireless shares ONE GStreamer pipeline for mic and speaker.**
  `media.stop_playing()` and `ReachyMini.cancel_move()` stall the microphone. After
  `audio.clear_player()`, always re-assert `media.start_recording()` (see `_clear_playback` in
  `reachy_openai_realtime/realtime.py` and `stop_current` in `motion/manager.py`).
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
- **Recorded moves are played by our own MotionManager loop, never `ReachyMini.play_move`.**
  `play_move`'s cancel path is `cancel_move()` → `media.stop_playing()`, which stalls the shared
  Wireless mic pipeline. Sidecar emotion sounds are skipped for the same reason (the speaker
  belongs to the Realtime audio path). Catalog names are sanitized (`^[A-Za-z0-9 _-]{1,64}$`)
  before they enter session instructions — dataset filenames are third-party input.
- **Two robots, one hostname.** Daytime robot `192.168.23.184`, night robot `192.168.200.128`,
  both `pollen@` / hostname `reachy-mini` with different host keys — a "HOST IDENTIFICATION
  CHANGED" warning between them is expected, but compare fingerprints before accepting.
- **Daemon API sharp edges** (port 8000): `POST /api/daemon/start` requires `?wake_up=true|false`
  (422 without it); job-status JSON embeds raw control chars — `tr -d '\000-\010\013-\037'`
  before jq; a crashed app STAYS in the app slot serving a stale error and refusing new starts —
  `POST /api/apps/stop-current-app` first, and always read POST response bodies.
- **`response_cancel_not_active` is a session killer if the watchdog ignores it.** Barge-in near
  speech end races `response.cancel` against server-side completion; the error IS the ack (no
  active response). `_handle_cancel_race_error` disarms the `response_cancel` watchdog — without
  it, WatchdogTimeout reconnects the whole session 3s after every late barge-in.
- **The model does what the instructions favor, not what the tool list offers.** With express
  described as "the" emotional reaction and recorded names given as a bare list, play_emotion
  never fired once on hardware. Steering needs all three: enum names in the tool schema, an
  explicit prefer-recorded instruction, and honest tool descriptions (express = subtle accent).
- **Starting the app does not wake the robot — a sleeping robot stays in its shell, "breathing".**
  App start while motors are `disabled` leaves the head down; MotionManager then animates around
  the sleep pose it booted into, and the app's robot-lock target stream overrides any external
  `wake_up` move (issue #25). Wake sequence that works over the daemon API:
  `POST /api/apps/stop-current-app` → `POST /api/motors/set_mode/enabled` →
  `POST /api/move/play/wake_up` (confirm `/api/state/present_head_pose` z ≈ 0, not ≈ −47mm) →
  `POST /api/apps/start-app/...`. `daemon/start?wake_up=true` is a no-op if daemon state is
  already `running`; asleep = `backend_status.ready:false` + `motor_control_mode:"disabled"`.
- **`POST /api/apps/update/{app}` refuses while that app is running** ("Cannot update ... while
  it is running. Please stop it first."). Deploy order: `stop-current-app` → update job →
  verify awake → `start-app`. An update fired into an empty app slot works directly.
- **Shutdown has a built-in ritual, symmetric with start.** `POST /api/move/play/goto_sleep`
  tucks the head into the shell AND then suspends the backend (`ready:false`, motors
  `disabled`) — one call is a full soft shutdown once the app is stopped. `POST
  /api/daemon/stop` requires `?goto_sleep=true|false` (422 without it), mirroring
  `daemon/start?wake_up=`. Sleep pose signature: head z ≈ −47mm, pitch ≈ 0.47 rad.
