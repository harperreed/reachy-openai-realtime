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
  CHANGED" warning between them is expected, but compare fingerprints before accepting. Before
  any robot action, restate the resolved label and IP; a correction from daytime to nighttime must
  be applied before issuing the command.
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
- **A healthy wake-enabled app starts with `connected:false` and `presence:"sleeping"`.**
  `scripts/robot start`/`deploy` must accept either that wake-armed state or the legacy
  `connected:true` state; waiting only for Realtime always times out after issue #12.
  `RuntimeStatus.set_presence` clears a stale `connected:true` when presence returns to
  sleeping; the stopped session does not write a disconnected phase on its stop path.
- **`POST /api/apps/update/{app}` refuses while that app is running** ("Cannot update ... while
  it is running. Please stop it first."). Deploy order: `stop-current-app` → update job →
  verify awake → `start-app`. An update fired into an empty app slot works directly.
- **Shutdown has a built-in ritual, symmetric with start.** `POST /api/move/play/goto_sleep`
  tucks the head into the shell AND then suspends the backend (`ready:false`, motors
  `disabled`) — one call is a full soft shutdown once the app is stopped. `POST
  /api/daemon/stop` requires `?goto_sleep=true|false` (422 without it), mirroring
  `daemon/start?wake_up=`. Sleep pose signature: head z ≈ −47mm, pitch ≈ 0.47 rad.
- **Wake word ("hey reachy") is Phase 2, issue #12.** Spec
  `docs/superpowers/specs/2026-08-21-wake-word-design.md`, plan
  `docs/superpowers/plans/2026-08-21-wake-word.md`. While asleep a `WakeWordWorker` runs an Edge
  Impulse `.eim` classifier and a `PresenceManager` owns BOOTING→SLEEPING→WAKING→AWAKE (+ERROR);
  on wake it opens one RealtimeRobotSession seeded with captured pre-roll audio and tears it down
  on sleep. Code under `reachy_openai_realtime/presence/`, `/wakeword/`, `/audio/`. `wake_enabled`
  defaults TRUE. Env vars are `REACHY_OPENAI_REALTIME_WAKE_*`; manual control is `POST
  /api/presence/wake` and `/api/presence/sleep`.
- **`main.py:run()` has TWO mutually exclusive paths.** `wake_enabled=true` (default) runs the
  PresenceManager; `wake_enabled=false` runs the original Phase-1 always-connected supervisor loop
  (preserved byte-for-byte). `_build_wake_detector` returns None on ANY failure (unknown backend,
  WakeModelError, or any other Exception, which it logs), and a None detector still boots: the
  manager goes SLEEPING then ERROR and stays reachable for manual wake. Wake setup NEVER crashes
  startup — don't add a raise that would.
- **Presence emits ONE `presence.transition` event, not the five names spec §27 lists.** It
  carries `from_state`/`to_state` (the edge), matching the `fsm.transition` convention, and
  `to_state` is greppable. Do NOT split it back into five `presence.*` events — that was a
  deliberate one-source-of-truth call (`presence/manager.py:_handle_transition`). Grepping for
  `presence.sleeping` etc. finds nothing by design.
- **Presence lock order: `_lock` before the state-machine lock, never the reverse.**
  `PresenceStateMachine.transition()` releases the state lock BEFORE firing its callback, and
  `_on_wake`/`request_wake` call `transition()` while holding `_lock`, so `_handle_transition`
  runs with `_lock` held by the same thread. Anything wired into the transition hook (event
  recording, `RuntimeStatus.set_presence`) must NOT take `_lock` or it self-deadlocks.
- **Wake audio is memory-only and never logged.** The pre-roll ring buffer (`AudioRingBuffer`)
  holds a few seconds of frames in RAM and is never written to disk; sleeping audio is never
  logged; `debug_capture_wake_audio` defaults false; no raw mic audio appears in any HTTP or UI
  surface. Nothing streams to OpenAI until a wake fires. This is a spec privacy boundary — keep it.
- **One microphone owner: `CaptureWorker` fans out to bounded drop-oldest subscriptions.** The
  realtime session and the wake worker are two `subscribe(name, max_buffer_ms=…)` consumers of the
  single CaptureWorker (`audio/capture.py`); a slow consumer drops its own oldest frames and can't
  stall capture or starve the other. Add consumers via subscribe/unsubscribe, never a second mic
  reader.
- **The wake `.eim` model is fetched at runtime, not vendored.** `wakeword/model_download.py` pins
  one Hugging Face revision + size + sha256 (the Space has no redistribution license), so first
  boot with wake enabled downloads ~13.5 MB into the config models dir. `ensure_wake_model` wraps
  every failure (network, disk, chmod, `os.replace`, hash mismatch) in `WakeModelError` and leaves
  no partial behind. The lone OSError it still lets through raw is `directory.mkdir`, which
  `_build_wake_detector`'s broad except neutralizes into graceful degradation.
- **Setting the session stop flag does NOT tear down an actively-engaged Realtime session** (FIXED
  on `main` commit `3036352`, deployed to the night robot 2026-08-23) — the bug: `POST /api/presence/sleep`
  could not reliably sleep a robot mid-conversation. `_run_connection` blocked on a bare
  `await asyncio.gather(*tasks)` over six tasks; two — `_watchdog_loop` (no stop param) and
  `_supervisor_loop` (`while True`) — never check the stop flag, and `gather(return_exceptions=False)`
  unblocks only when ALL tasks finish OR one raises. The stop-honoring tasks (`_record_loop`,
  `_event_loop`) RETURN rather than raise, so flipping `session_stop`/`app_stop` never unblocked the
  gather. Teardown fired only when the OpenAI connection closed (event-loop's `async for` raises) or
  the supervisor's 120 s FSM-inactivity tripped (`session/supervisor.py`). While ambient noise kept
  the FSM transitioning and the socket alive, NEITHER fired → sleep returned `{ok:true,state:sleeping}`
  but the robot stayed AWAKE, talking to the empty room and burning tokens. Manual app-stop "works"
  only because the daemon SIGKILLs the process after ~20 s, hiding the graceful-teardown failure.
  The fix: `_run_connection` now awaits `_await_tasks_or_stop(tasks, stop_event)`, which races the
  gathered task group against a stop-poller and returns the instant the flag flips (~50 ms); the
  existing `finally` then cancels the still-running loops. Wait-for-all / first-exception semantics
  are preserved, so the reconnect path is untouched (`tests/test_realtime_teardown.py`). Recovery on
  a pre-fix build: `POST /api/apps/stop-current-app`. Found on night robot 2026-08-22 (manual wake
  worked; manual sleep hung; had to stop the app).
- **Anti-runaway backstop: two mechanisms, both bail by SETTING the stop flag, never raising.**
  This version was deployed on 2026-08-23 and proved insufficient on 2026-08-24. Root cause of the
  runaway: the ReSpeaker fires `speech_detected` on ~27% of ambient noise, so the turn-gate opens on
  noise, commits noise "turns," and fires `response.create` per turn with nothing bounding it — it
  runs to the 60-min cap, reconnects, and continues. Two independent backstops now bound it:
  (1) **Wall-clock ceiling** — the hard "never runs away" guarantee, no classification. In
  `_supervisor_loop`, once the whole session outlives `noise_bail_session_minutes` (default 30) it
  sets the stop flag. Measured from `_session_started_at`, which is set once at `run()` start and
  deliberately NOT reset in `reset_connection_state`, so it survives a reconnect storm (distinct from
  the per-socket `_connected_at`). `noise_bail_session_minutes=0` disables it.
  (2) **Transcript counter** — the fast bail. `_is_garbage_transcript` flags a committed turn that
  transcribes to no word characters (empty, or only punctuation/symbols; `\w` is Unicode so CJK like
  "好" is spared). `_note_user_transcript` counts consecutive wordless turns and at `noise_bail_turns`
  (default 3) sets the stop flag; a real-word turn resets the count. This counter ALSO survives
  reconnect on purpose (mirrors the ceiling) — not in `reset_connection_state`. `noise_bail_turns=0`
  disables it. Both mechanisms depend on the teardown fix `3036352`: setting the stop flag only
  sleeps a live session because `_run_connection` races the task group against the flag. Raising
  instead would drive the reconnect path (wrong for a noise bail — you want stop→sleep, wake-armed).
- **The deployed transcript guard does not bound spend and its production stop signal is broken.**
  Night Reachy produced 25 response requests in about three minutes because Whisper returned
  word-like noise fragments such as `you`, resetting the wordless counter. `_EitherStop` also lacks
  the `set()` method both guards call, so a threshold would raise rather than cleanly sleep. The
  approved replacement is a transcript-free five-turn/60-second breaker checked before commit,
  followed by sleep latched against wake words until manual wake or app restart. Keep night Reachy
  stopped until that replacement passes review and hardware acceptance.
- **The transcript bail needs input transcription ON, which is metered per committed turn — NOT free.**
  `_session_config` enables `audio.input.transcription` (`input_transcription_model`, default
  `whisper-1`; blank disables). OpenAI then emits `conversation.item.input_audio_transcription.completed`
  per committed turn (its `usage` field bills tokens on EVERY turn, real speech included, not just
  garbage). The user transcript is recorded to in-memory `RuntimeStatus` for the dashboard but is NOT
  written to the file log — room speech stays memory-only, unlike Reachy's own output which logs at INFO.
- **Below the bail threshold the robot still speaks ≤N-1 noise replies before sleeping.** `response.create`
  fires when a noise turn commits, before its transcript arrives, so turns 1..N-1 produce spoken replies
  (bounded, then sleep). Cancelling the in-flight response on a garbage transcript (reusing the barge-in /
  `_interrupted_response_ids` machinery) was evaluated and DEFERRED: it races the already-started response
  and risks self-interrupting a real brief utterance, for the marginal gain of silencing ≤2 short replies.
