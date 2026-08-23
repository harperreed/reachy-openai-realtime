# Juno's proposal — noise bail

**Worldview:** "A robot that naps rudely is worse than one that babbles. The bail
and the return ARE the product." I optimize for how it feels to be cut off and to
re-engage.

---

## Slot 1 — Detection

**Pick: consecutive-noise-turn counter with an angle-stability veto.**

No new signals, no new infrastructure — lean entirely on what's already wired.

Every time `decision.stopped` fires (a turn commits), increment a per-session
counter `_noise_turn_count`. Reset it to zero on any turn that produces a
*non-trivial* response (see below). If the counter reaches **N = 3**, trigger the
sleep sequence described in Slot 2.

A "noise turn" is one where **all** of the following are true at commit time:

1. `doa_speech_detected` was **never `True`** during the turn (logged via
   `status.record_audio_sample`; a per-turn boolean flag `_turn_had_doa_true`
   tracks this, reset on `decision.started`).
2. The DoA angle moved by more than 30° between start and stop of the turn
   (angle-stability veto — a real person in a fixed spot doesn't teleport).
3. The turn lasted **less than 3 s** (ambient triggers are usually short bursts;
   real speech rarely is).

If transcription is off (it is today), we can't read the text to classify
"trivial." Instead, use turn duration + DoA stability as the sole signal. Three
consecutive turns that fail the DoA-stability and duration tests is extremely
unlikely from a real person; it is exactly what wandering ambient sound looks like.

**What changes:**
- Add `_noise_turn_count: int`, `_turn_had_doa_true: bool`, `_turn_start_angle:
  float | None` to `RealtimeRobotSession.__init__`.
- Set `_turn_had_doa_true = False`, `_turn_start_angle` = current DoA angle on
  `decision.started`.
- Set `_turn_had_doa_true = True` when `doa_speech_detected is True` during the
  record loop.
- On `decision.stopped`, evaluate the three conditions above; increment or reset
  the counter; if counter >= N, raise a `NoiseBailSignal` (a small sentinel
  exception, or set a flag and let `_supervisor_loop` act on it — I prefer the
  latter to keep the record loop clean).
- Track `_noise_turn_count` in `runtime_status` via `record_event` so it's
  observable in the flight recorder.

**Why not transcript-based?** Enabling `input_audio_transcription` costs tokens on
every commit, adds latency, and is a new product decision. The DoA + duration
signal is free, already sampled, and matches the observed failure mode exactly.

**Why N = 3?** One false positive is fine (bail-toward-sleep is the mandate). Two
in a row is suspicious. Three consecutive bad turns is a running away scenario in
progress, and three consecutive false positives from a real person in 9–60 s of
conversation is vanishingly unlikely.

---

## Slot 2 — Response (attack hard)

**The experience of bailing is the product. Three stages.**

### Stage 0 — Growing doubt (turns 1 and 2, silent)

On the first and second suspected-noise turns Reachy does nothing visible. It
continues to listen. No output, no spoken response, no committed API call. This
avoids the original runaway (no `response.create` for noise turns) while not
alarming a person who happened to sneeze or shuffle.

Concretely: after `input_audio_buffer.commit()` succeeds but before
`connection.response.create(...)`, check the noise evaluation. If this is a noise
turn (not yet at threshold), skip the `response.create` entirely. The buffer is
already committed; the model never sees it as a prompt for a reply. The FSM still
transitions through WAITING_RESPONSE → LISTENING normally (the response-done flag
resolves immediately since no response was requested).

**Motion during Stage 0:** Reachy's head slowly tilts a few degrees, as if
listening harder. This is the `motion.set_idle_enabled(True)` path already running
— no new motion call needed. The ambient idle motion signals mild attention without
committing to engagement.

### Stage 1 — Soft acknowledgment (turn 3, the threshold)

On the third consecutive noise turn Reachy speaks one quiet line and begins its
sleep transition:

> **"I'll rest. Say 'hey Reachy' when you need me."**

This is a real `response.create` with a dedicated `response_instructions` override
that contains only the farewell phrase and instructs the model to say nothing else.
After the audio completes, the session sets its stop flag (the teardown fix on
`3036352` ensures this actually ends the session).

**Motion:** as the farewell phrase plays, Reachy's head slowly bows — the same
neutral-rest pose used after a sustained idle timeout. The motion is driven by
`motion.set_speaking_enabled(False)` + `motion.set_idle_enabled(False)` after
playback, which already exists in the teardown path. No new motion API needed.

**Why speak at all?** A sudden silence after three noise turns is confusing if
there was a real person — they hear Reachy stop responding and don't know why.
One calm, conversational sentence closes the loop. It also seeds the expectation
that "hey Reachy" will work — which is the entire re-engagement path.

**Why not a longer statement?** Orwell's Rule 3. One sentence is enough.

### Stage 2 — Sleep (immediately after Stage 1 audio drains)

The session tears down. Presence goes to SLEEPING. The wake-word listener comes
live. The robot is physically at rest.

No "I'm going to sleep now... waiting... actually sleeping... okay, sleeping..."
ceremony. The farewell is the last word. Silence follows immediately.

### Re-engagement

Wake word detected → the existing wake sequence fires. Reachy's head lifts, the
boot greeting plays ("Hello. Talk to me."), the session opens fresh. The human
never sees "reconnecting" or "please wait" — just the robot waking to greet them.

**This must feel like meeting someone again, not rebooting a device.**

The framing in `greeting_instructions` already supports this. If the memory system
is live, the wake block seeds context; the human might feel recognized. That's the
re-engagement dividend of sleeping gracefully.

---

## Why (from my worldview)

The runaway failure — Reachy talking to itself in a loop, deaf to real speech —
is the worst failure mode not because it wastes API tokens but because it breaks
trust. Someone walks into the room, tries to say something, and the robot is too
busy to hear them.

Sleeping early, even if it means occasionally cutting off a very quiet person, is
correct. But sleeping rudely — going silent mid-interaction, or disappearing
without a word — is also a trust break. The farewell phrase is not a nicety. It
is the designed experience of the cutoff.

The return must feel easy and natural because "say 'hey Reachy'" is a low bar and
the greeting is warm. If the re-entry felt like a cold boot, the human would blame
the sleep. Because it feels like a conversation resuming, they forgive the nap.

---

## Costs, risks, trade-offs

**Weak-mic tension:** A real person in a loud room whose DoA angle happens to
wander (reflected signal) could trigger a noise bail. N = 3 and the 3 s duration
floor mitigate this. At N = 3, the false-bail probability is low enough that the
bail-toward-sleep mandate accepts it explicitly.

**The skipped `response.create` (Stage 0):** The committed audio buffer sits
unacknowledged. This is safe — the Realtime API allows committed buffers without a
corresponding response request; they are cleared on session reconnect. No API
state corruption.

**The farewell response.create (Stage 1):** This is one real API call on a noise
turn. Cost: minimal (short completion, no tools). Risk: the model ignores the
override instruction and says something different. Mitigation: the
`response_instructions` override is tightly scoped and the persona already
produces short, compliant replies.

**No `cancel_move` / `media.stop_playing`:** The farewell plays to completion
before teardown. The stop flag is only set after the speaker drains, using the
same speaker-busy pattern already in use at `_speaker_busy_until`. Constraint
respected.

**Interaction with 120 s FSM inactivity:** Stage 0 turns (no `response.create`)
do not reset `_last_fsm_transition_at` in a way that prevents inactivity teardown
— the FSM still transitions (USER_SPEAKING → WAITING_RESPONSE → LISTENING), so
inactivity IS reset. This means a sustained noise environment won't hit the
supervisor timeout; it will hit our N=3 counter first (seconds, not 120 s). That
is the correct behavior.

---

## How I'd test it (TDD — failing test first)

**Test 1 — No response on a noise turn (Stage 0):**

```python
# tests/test_noise_bail.py
def test_noise_turn_skips_response_create(mock_session):
    """Three conditions met: no doa_true, angle jump, short turn.
    response.create must NOT be called."""
    simulate_noise_turn(mock_session, doa_true=False, angle_delta=45, duration_ms=1200)
    assert mock_session.connection.response.create.call_count == 0
```

**Test 2 — Counter resets on clean turn:**

```python
def test_noise_turn_counter_resets_on_real_speech(mock_session):
    simulate_noise_turn(mock_session, ...)
    simulate_noise_turn(mock_session, ...)
    simulate_real_turn(mock_session, doa_true=True, angle_delta=5, duration_ms=4500)
    simulate_noise_turn(mock_session, ...)
    assert mock_session._noise_turn_count == 1  # reset after real turn
```

**Test 3 — Stage 1 fires at N = 3 and sets stop flag:**

```python
def test_three_noise_turns_trigger_farewell_and_stop(mock_session):
    for _ in range(3):
        simulate_noise_turn(mock_session, ...)
    # farewell response.create was called exactly once, with bail instructions
    assert mock_session.connection.response.create.call_count == 1
    assert "hey reachy" in get_instructions_arg(mock_session).lower()
    assert mock_session._stop_event.is_set()
```

**Test 4 — DoA angle stability alone is not enough; all three conditions required:**

```python
def test_stable_angle_turn_not_classified_as_noise(mock_session):
    simulate_noise_turn(mock_session, doa_true=False, angle_delta=5, duration_ms=1200)
    assert mock_session._noise_turn_count == 0
```

These four tests, written first, define the contract. Passing them means the
detection and response logic behaves correctly without touching the VAD internals
or adding mock modes to production code.
