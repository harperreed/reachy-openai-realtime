# Sarge's Proposal — Noise Bail

**Worldview:** every second connected to a noisy room is wasted money and heat.
Sleep early, sleep often, wake instantly.

---

## Slot 1 — Detection: consecutive-noise turn counter

**Signal:** count committed turns that look like noise. A turn is noise when it
starts, runs, and stops with no DoA confirmation — i.e. `doa_speech_detected`
never went solidly `True` during the capture window AND the committed audio
falls below a short-duration floor (< 800 ms of actual speech energy, since
real speech rarely trips the VAD and stops that fast).

**Threshold:** 3 consecutive noise turns → bail. At a typical noise-storm
cadence that is under 30 seconds of real time. Default: `noise_bail_turn_count=3`,
env-overridable as `REACHY_OPENAI_REALTIME_NOISE_BAIL_TURNS`.

**Where it lives:**

- `AppConfig` gets `noise_bail_turn_count: int = 3` with `from_env` wired.
- `_record_loop` in `realtime.py` tracks `_consecutive_noise_turns: int = 0`.
  - On `decision.stopped`: if the turn was noise (logic below), increment;
    reset to 0 on any turn where DoA confirmed speech or committed duration ≥ 1.5 s.
  - At count == `noise_bail_turn_count`: raise a sentinel or set the stop event.

**Noise classification per turn (all local, zero cost):**

```
noise = (
    max_doa_speech_confirmed_during_turn is False   # DoA never saw human
    and committed_audio_ms < 1500                   # very short burst
)
```

`max_doa_speech_confirmed_during_turn` is a flag reset on `decision.started`,
set to `True` the first time the live `doa_speech_detected` value is `True`
(not `None`) during the capture window. This reuses the existing DoA poller —
no new polling thread.

**Why this signal?**

The ReSpeaker's `speech_detected` bit fires on ambient sound (confirmed live),
but it flickers — short random bursts, angle jumping. A real person produces a
sustained window where `speech_detected is True` at some stable-ish angle. Three
noise turns in a row is a streak that room noise produces routinely and a person
almost never does. Cheap: one integer and one boolean per turn.

What I am explicitly NOT doing: no transcript analysis (costs a model call),
no ReSpeaker angle-variance math (adds complexity for marginal gain), no
per-frame energy histogram (overkill). The counter is the minimum viable signal.

---

## Slot 2 — Response: immediate stop-event, back to wake-armed sleep

**On hitting the threshold:**

1. Set the session stop event. The teardown fix (`3036352`) makes this
   actually terminate the engaged session — that's the whole reason we can do
   this at all. No new teardown logic needed.
2. Do NOT call `response.create` for the triggering turn. The turn counter
   fires inside the `decision.stopped` block, before the `response.create`
   call. Short-circuit with a `return` (or `raise _NoiseBailSignal`) so the
   model never sees that audio. No paid reply for the turn that trips the bail.
3. The supervisor/main loop sees the stop and transitions back to the sleep /
   wake-word-armed state exactly as a normal idle teardown does. No new paths.
4. Log it loudly: `logger.info("noise bail: %d consecutive noise turns — sleeping", n)`.
   Record an `add_event` to `runtime_status` so the dashboard shows it.

**Re-arm:** wake word fires re-engagement exactly as always. Zero new code for
that path.

**No grace period, no "are you sure?"** — the owner accepted false positives.
A person who was talking gets a robot that went to sleep. They say "hey reachy"
and it wakes. That's the designed flow.

---

## Why (from Sarge's optimization function)

Today's runaway: 60 minutes of open WebSocket × ~N turns/minute × response
token cost per turn. With a noise-storm cadence of say 4 turns/minute, the
first 3 turns (45 seconds) are already paid; after bail we save 59+ minutes of
connection and every subsequent turn's cost. Against the savings, the false
positive cost is one "hey reachy" utterance from a real user. That's a
catastrophically good trade.

Three turns is the right number because: 1 is too hair-trigger (any single
stutter trips it), 2 is marginal, 3 means the robot already answered noise once
before bailing (one paid turn wasted on the way out, acceptable), and 4+ lets
the runaway breathe longer than it should.

The bail fires BEFORE `response.create` on the triggering turn, so the worst
case is 2 paid noise replies (turns 1 and 2) before the plug is pulled on turn
3. Today the worst case is 60 minutes of paid replies.

---

## Cost, risks, trade-offs

**False positive — cuts off a real person.** Unavoidable with aggressive bail.
Worst scenario: person speaks normally, DoA flickers (bad angle, reverb), turn
reads as noise three times running. Result: robot goes to sleep mid-conversation.
Mitigation: the 1500 ms floor on committed audio length. A sentence is almost
always longer. A real question rarely finishes in under 1.5 s of committed
audio; noise bursts typically are much shorter. This is not a hard guarantee,
only a bias. Owner accepted this.

**DoA unavailable (robot without ReSpeaker or stale read).** `DoAPoller.latest()`
returns `None` when stale. If DoA is always `None`, the noise flag falls back
to duration-only (`committed_audio_ms < 1500`). That's weaker but still catches
the sub-second noise bursts that most reliably indicate machine noise rather than
speech. The system degrades gracefully, not silently open.

**Counter reset policy.** Any single "real" turn (DoA confirmed or long enough)
resets to 0. A person who successfully communicates once, then the room gets
noisy, gets another 3-turn budget before bail. Correct behavior.

**Interaction with the 120 s FSM inactivity teardown.** Noise keeps the FSM
active, so that teardown never fires — that's the confirmed bug. The bail counter
is orthogonal and fires independently. Both can coexist; bail fires first in a
sustained noise storm.

**No new connected-time cost.** Detection is local arithmetic. We do not wait
for a model response to decide whether a turn was noise.

---

## How I'd test it (TDD — failing test first)

**Test 1 (unit, fails first):** `test_noise_bail_triggers_stop_after_threshold`

Feed `_record_loop` (or the relevant extracted method) a sequence of
`decision.stopped` events all classified as noise turns. Assert that after
`noise_bail_turn_count` such turns the stop event is set and `response.create`
was NOT called on the triggering turn. Use a mock connection that records calls
to `response.create` and raises if called more than `count - 1` times.

This test is meaningful: it asserts real behavior (stop event set, paid call
avoided) and will fail until the counter and the short-circuit are implemented.

**Test 2 (unit):** `test_noise_bail_resets_on_real_turn`

Three noise turns, then one turn with DoA confirmed (`doa_speech_detected=True`
during capture), then two more noise turns. Assert stop event is NOT set after
five total turns (counter reset mid-sequence).

**Test 3 (unit):** `test_noise_bail_skips_when_doa_unavailable_but_long_audio`

DoA always returns `None`. A committed turn with 2000 ms of audio. Assert it
does NOT count as noise (duration floor protects it).

**Test 4 (config):** `test_appconfig_noise_bail_env_override`

Set `REACHY_OPENAI_REALTIME_NOISE_BAIL_TURNS=5`. Assert `AppConfig.from_env()`
returns `noise_bail_turn_count=5`.

All four tests are cheap, target real application state, and use no mocks on
external APIs.
