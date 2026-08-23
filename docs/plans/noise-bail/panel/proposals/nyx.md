# Nyx — Noise-Bail Proposal

**Worldview:** every detector fails eventually. the only thing that saves you is a hard ceiling.

---

## Slot 1 — Detection (best-effort, cheap)

**Signal: consecutive no-reply turns.**

After each `response.create` on `decision.stopped`, the model answers. A real human follow-up resets the counter. Noise produces a reply with no human follow-up — the session flips back to LISTENING, silence, another noise trigger, another reply, repeat. The detector sees this pattern: N consecutive turns where the user never spoke again between when the previous response finished and when the next turn committed.

Concretely: count turns that commit (`decision.stopped`) **while the prior response was still the last interaction** — meaning no human turn interrupted the response and no barge-in was recorded before the new turn started. Call these "unacknowledged turns."

This is not transcript-based (no transcription cost). It uses state already tracked: `_response_generation_done`, `_last_fsm_transition_at`, the FSM. Each time `decision.stopped` fires, check: did a real barge-in or a new USER_SPEAKING epoch arrive since the last response completed? If not, increment `_noise_turn_count`. If yes (a real person interrupted, or spoke after the response), reset to zero.

The counter lives in `RealtimeRobotSession`. Add one integer `_noise_turn_count: int = 0`. Reset it when:
- `_interrupt_assistant` is called (human barge-in)
- `decision.started` fires from LISTENING state (human started speaking while idle — not during assistant playback, where noise is filtered more strictly)

Increment it when `decision.stopped` fires and the turn was NOT preceded by a `decision.started` from LISTENING state in this turn cycle. Wait — actually simpler: set a flag `_last_turn_was_human: bool = False`. When `decision.started` fires from `SessionState.LISTENING` (the quiet between-turns state), set `_last_turn_was_human = True`. When `decision.stopped` fires, if `_last_turn_was_human` is False, increment `_noise_turn_count`; if True, reset `_noise_turn_count = 0`. Then reset `_last_turn_was_human = False`.

This is ~15 lines in `_record_loop`. No new imports. No transcription. No NLP. It fails open — if the detector is wrong and counts a real interaction as noise, the ceiling (Slot 2) just sleeps the robot a little early.

### What this catches and misses

Catches: sustained ambient noise triggering VAD in the between-turns quiet, which is the observed runaway.

Misses: a very loud room where real speech and noise turns interleave (detector keeps resetting). That's fine — the ceiling still bounds the total session length (Slot 2b below).

---

## Slot 2 — Response (the hard ceiling, unkillable)

Two independent counters. Both trigger the same action: set `session_stop`, which propagates through `_EitherStop` and `_await_tasks_or_stop` (the teardown-fix foundation), ending the session and returning to SLEEPING.

### Slot 2a — Per-session turn budget

**N = 8 consecutive no-human turns → bail.**

Eight because:
- The runaway produces a turn every ~5–10 seconds (VAD silence timeout 800ms + model latency + brief quiet).
- Eight turns = 40–80 seconds of runaway before bail. That is survivable in a demo.
- Eight is enough that a real human doing one interaction then going quiet for a long time is unlikely to trip it by accident (they'd have to cause 8 VAD fires in a row with zero barge-in). A real person in the room who triggered the wake word is expected to speak.
- Below 5 and you start cutting off real shy/slow users. Above 12 and you're tolerating minutes of runaway.

Env-override: `REACHY_NOISE_BAIL_TURN_LIMIT` (int, default 8). Validated into `AppConfig`.

### Slot 2b — Absolute session wall clock

**30 minutes → bail regardless.**

The supervisor's 120s FSM-inactivity teardown does not fire during runaway (noise keeps FSM active). The reconnect loop has no ceiling either — it reconnects indefinitely. OpenAI caps at 60 minutes but we hit that and reconnect.

A 30-minute wall clock is the unkillable backstop. A normal Reachy interaction is a short conversation — 2–10 minutes at the most. If the session has been alive 30 minutes something has gone wrong, full stop. Bail to sleep.

Implementation: in `_supervisor_loop` (already a long-running polling loop), check `time.monotonic() - self._connected_at` on each poll cycle. When it exceeds the limit, set `session_stop` and raise (same `WatchdogTimeout` path, or add a new `NoiseBailout` exception — either works). `_connected_at` is already set at the top of `_run_connection`.

The cleaner hook is `_supervisor_loop` because it already owns the "something wedged" teardown logic, polls every `SUPERVISOR_POLL_SECONDS`, and already raises to trigger a clean disconnect+sleep. One additional check branch.

Env-override: `REACHY_NOISE_BAIL_SESSION_MINUTES` (int, default 30). Validated into `AppConfig`.

### Slot 2 action — bail to sleep

When either ceiling fires:

1. Set `session_stop` (the `_EitherStop` secondary event the session already watches).
2. The teardown-fix (`3036352`) ensures `_await_tasks_or_stop` unblocks immediately.
3. `session.run` returns `SessionOutcome.STOPPED`.
4. `_run_session` reaches its `finally`, calls `_finish_session`.
5. `_finish_session` transitions `AWAKE → SLEEPING` and calls `sleeping_pose()`.
6. The wake-word worker resumes. Robot waits for "hey reachy."

No reboot. No reconnect. The PresenceManager loop continues watching the app stop event — the robot is properly asleep and re-wakeable. This is exactly the bail-toward-sleep bias.

**Do not reconnect on noise bail.** The reconnect loop in `session.run` is for connection errors. A noise bail should set `session_stop`, not raise an exception — which causes `run()` to return `SessionOutcome.STOPPED` and lets the presence manager handle sleeping. Adding a `SessionOutcome.NOISE_BAIL` variant gives the presence manager a clean signal if it wants to log differently.

---

## Why

Detection (Slot 1) is intentionally dumb: no NLP, no transcription, no energy heuristics. It measures one thing — does a human interact with the model after each turn? — which is the only ground truth we have. It fails open (false positive = early sleep, acceptable per product bias).

The ceiling (Slot 2) does not trust the detector. Eight consecutive noise turns fires regardless of how sophisticated or naive the detector is. Thirty minutes fires regardless of whether turns are being counted correctly. The system has two independent chances to stop. The worst case is bounded: 8 turns ≈ 80s runaway, or 30 minutes total, whichever comes first. Never runs away forever.

The key insight: the 120s FSM inactivity teardown was the right shape of fix but the wrong signal. It checks "has the FSM been stuck in a non-LISTENING state." Noise keeps the FSM moving — so that check is blind to the runaway. The per-turn counter watches the FSM transitions that matter (USER_SPEAKING → WAITING → LISTENING → USER_SPEAKING without human intent) and the wall clock catches everything the counter misses.

---

## Cost, risks, trade-offs

**False positives (early bail on a real user):** a shy user who triggers the wake word and then waits silently while ambient noise fires 8 turns would get bailed on. Mitigation: a real user is expected to respond to the greeting; if they don't, sleeping is the right call. The wake word is the frictionless re-entry.

**False negatives (detector misses runaway):** a very noisy room where real human speech and noise turns alternate. The 30-minute wall clock still catches this. A session can run away for at most 30 minutes.

**Weak-mic tension:** the bail-toward-sleep bias already accepts cutting off real users in noisy rooms. The 8-turn threshold is loose enough that a single interaction resets it.

**No transcription cost:** this proposal deliberately avoids enabling `input_audio_transcription`. That would add latency and token cost to every turn. The counter approach is free.

**Interaction with reconnect loop:** the noise bail must not be confused with a connection error. Using `session_stop.set()` (not raising) guarantees `SessionOutcome.STOPPED` and prevents the reconnect loop from spinning up another session straight into more noise.

**`_connected_at` is None before socket open:** wall-clock check in `_supervisor_loop` must guard with `if self._connected_at is not None`. Already done for the existing metrics gauge.

---

## How to test (TDD — failing tests first)

### Test 1 — turn counter increments on noise turns

```python
# tests/test_noise_bail.py
def test_noise_turn_counter_increments_without_human_interaction():
    # Build a minimal RealtimeRobotSession with a fake connection.
    # Fire decision.started then decision.stopped N times from LISTENING state
    # without setting _last_turn_was_human.
    # Assert _noise_turn_count == N after N noise turns.
```

### Test 2 — counter resets on barge-in

```python
def test_noise_turn_counter_resets_on_human_barge_in():
    # After 5 noise turns, call _interrupt_assistant path (or set the flag).
    # Assert _noise_turn_count == 0.
```

### Test 3 — bail fires at threshold

```python
def test_noise_bail_sets_session_stop_at_turn_limit():
    # session_stop = threading.Event()
    # Drive noise turns to N (config default 8).
    # Assert session_stop.is_set().
```

### Test 4 — wall-clock bail in supervisor loop

```python
async def test_supervisor_bails_after_session_wall_clock():
    # Create session, set _connected_at = time.monotonic() - (30 * 60 + 1).
    # Run _supervisor_loop for one tick.
    # Assert it raises (or sets session_stop, depending on implementation choice).
```

### Test 5 — no bail when human speaks

```python
def test_no_noise_bail_when_human_speaks_each_turn():
    # Alternate: decision.started (from LISTENING, human), decision.stopped.
    # After 20 turns, assert _noise_turn_count == 0 and session_stop not set.
```

All tests use real state transitions, no mocks testing mocks. Session stop is inspectable as a `threading.Event`.

---

## Summary of changes

| File | Change |
|---|---|
| `config.py` | Add `noise_bail_turn_limit: int = 8`, `noise_bail_session_minutes: int = 30`, env overrides |
| `realtime.py` | Add `_noise_turn_count`, `_last_turn_was_human` to `__init__`; update `_record_loop` decision.started/stopped branches; update `_interrupt_assistant`; pass `session_stop` setter callback |
| `session/supervisor.py` | Add wall-clock check in `_supervisor_loop`; expose `NoiseBailout` exception or reuse `WatchdogTimeout` |
| `runtime_status.py` | Add `record_event("noise_bail.turn_limit")` / `record_event("noise_bail.wall_clock")` calls at bail sites |
| `tests/test_noise_bail.py` | All five tests above, written first |

Estimated size: ~60 lines of new application code, ~120 lines of tests.
