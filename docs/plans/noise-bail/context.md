# Jam context — stop Reachy from conversing with noise

## The problem (root cause, confirmed by direct observation)

Reachy Mini runs an OpenAI Realtime session with **server VAD off**
(`turn_detection=None`). The app drives its own turn-taking: a local energy
detector (`vad.py` `EnergyTurnDetector`) plus an optional ReSpeaker
Direction-of-Arrival (DoA) "speech detected" gate.

The robot cannot tell a person from room noise. Left awake in a noisy-enough
room it commits endless noise "turns," answers each one out loud ("I only
hear noise, I'll stay quiet"), never goes idle (noise keeps the state machine
active), runs to OpenAI's 60-minute session cap, reconnects, and continues.
Observed symptom: "it wouldn't shut up, it just kept talking, and it didn't
seem to hear what we said."

### The trigger (observed live on the robot, app stopped)

The `38fb:1001` "Reachy Mini Audio" USB device **is** the ReSpeaker (XVF3800
array with Pollen firmware — `init_respeaker_usb` probes for exactly this ID
as its preferred device). `robot.media.get_DoA()` is fully wired and returns
`(angle_radians, speech_detected)` or `None`. A live 15-sample read with
nobody addressing the robot returned `speech_detected=True` on ~4 samples,
with the angle jumping around — the ReSpeaker's speech bit fires on ambient
sound and localizes random noise, not a person.

### Why that runs away (two independent defects)

1. **The gate opens on noise.** In `realtime.py`'s record loop, a turn starts
   when the VAD sees `speech_detected is not False` AND energy clears the
   start threshold (`vad.py`, default ~-45 dBFS; `start_threshold =
   min(-38, max(-56, noise_floor+5))`, `noise_floor` default -50). Two holes
   in that one condition:
   - The ReSpeaker speech bit is `True` on ambient sound (observed).
   - `is not False` also opens on `None`, so any dropped/stale DoA read fails
     **open** — the "respeaker+energy" backend silently degrades to bare
     energy VAD. (`DoAPoller.latest()` returns `None` when the newest reading
     is older than 0.6s or absent.)
   During assistant playback the gate is stricter (`speech_detected is True`
   required) to stop self-interruption; the runaway is the between-turns case.

2. **Nothing bounds it.** On turn end (`decision.stopped`) the loop calls
   `input_audio_buffer.commit()` then `response.create(...)` with per-turn
   `response_instructions` — so every noise turn produces a spoken reply.
   The supervisor has a 120s FSM-inactivity teardown, but noise keeps the FSM
   transitioning, so it never fires. Auto-sleep is idle-based; noise = not
   idle. There is no "N useless turns in a row → stop" backstop.

## Design bias (decided by the product owner) — BAIL TOWARD SLEEP

The hard requirement is **never runs away**. Occasionally cutting off or
ignoring a real person in a noisy room is an **acceptable** price. Favor
aggressive, early sleep with frictionless re-wake over staying engaged.

## The two architectural slots

- **Slot 1 — Detection.** How do we decide a turn (or the ongoing session) is
  noise, not a person?
- **Slot 2 — Response.** What do we do about it once detected?

## Hard constraints (do not violate)

- **The teardown fix is the foundation.** A separate, already-written and
  unit-tested fix (`3036352`, on local `main`, not yet deployed) makes a
  mid-session stop actually end an engaged session — today, flipping the stop
  flag does NOT end the session because `_run_connection`'s `asyncio.gather`
  never unblocks when loops return cleanly. ANY "go to sleep / stop" response
  depends on that fix. Assume it is present; build on top of it. Do not
  redesign teardown.
- **TDD.** Tests first, asserting real behavior. No mocks-testing-mocks, no
  mocks in end-to-end paths.
- Python 3.10 floor. `ruff` line-length 110. Match surrounding style. Make the
  smallest reasonable change. One source of truth (no duplicated state).
- **Forbidden in app code:** `ReachyMini.cancel_move()`,
  `media.stop_playing()`, `media.audio.clear_player()`. Never expose or log
  the OpenAI API key or raw mic audio. Do not reboot the robot.
- Do not rebuild the whole VAD wholesale unless you can justify it as the
  smallest correct change.

## Facts a proposal may rely on

- **No user transcript today.** `input_audio_transcription` appears OFF (the
  logs show model-output transcripts, no user-side text). A transcript-based
  detector must enable it — real added cost. Not a new privacy boundary
  (audio already streams to OpenAI once a turn commits post-wake), but it
  surfaces recognized text.
- **Wake word exists.** `config.py`: `wake_enabled`, phrase "hey reachy",
  Edge Impulse `.eim` model, threshold 0.70. Presence sleep/wake exists.
  Re-engagement after a bail is the wake word.
- **Status plumbing exists.** `runtime_status.py` already records
  `vad_backend`, `doa_speech_detected`, `doa_angle_degrees`, energy levels —
  reuse it; don't invent a parallel telemetry path.

## Files in play

- `reachy_openai_realtime/realtime.py` — record loop (~555-714), `DoAPoller`
  (~72-131), session run/teardown (~343), `response.create` on turn stop.
- `reachy_openai_realtime/vad.py` — `EnergyTurnDetector` (thresholds, gate).
- `reachy_openai_realtime/config.py` — `AppConfig` (+ env overrides),
  prompt/instructions.
- `reachy_openai_realtime/runtime_status.py` — status fields.
- `reachy_openai_realtime/session/supervisor.py` — FSM inactivity teardown.
- `tests/` — pytest suite (currently green).

## Your deliverable

Write your proposal to `docs/plans/noise-bail/panel/proposals/<your-name>.md`
covering:
1. **Slot 1 (detection):** your recommended mechanism, concretely — what
   signal(s), what threshold/logic, roughly what code changes where.
2. **Slot 2 (response):** what happens on detection, concretely.
3. **Why**, from your worldview / optimization function.
4. **Cost, risks, trade-offs** — including how it interacts with the
   bail-toward-sleep bias and the weak-mic tension (too strict = deaf to real
   speech).
5. **How you'd test it** (TDD — what failing test comes first).

Propose YOUR approach independently. Do not hedge toward a committee answer.
