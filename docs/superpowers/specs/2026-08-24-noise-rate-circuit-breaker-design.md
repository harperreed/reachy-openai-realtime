<!-- ABOUTME: Design for a transcript-independent turn-rate circuit breaker that bounds runaway Realtime spend. -->
<!-- ABOUTME: A trip ends the session before another response and latches wake-word activation until operator action. -->

# Noise Rate Circuit Breaker Design

**Status:** Approved by Doctor Biz on 2026-08-24.

## Incident

Night Reachy entered repeated false conversation turns. Local VAD treated room audio, and possibly
speaker output, as user speech. Each completed false turn committed input and requested another
Realtime response. Model output then gave the microphone more audio to classify.

The flight recorder establishes two distinct periods:

- The first runaway produced 338 completed responses from 14:20–15:28 UTC on 2026-08-23. It
  predates deployment of the existing transcript and wall-clock guards.
- After those guards were deployed, a later session produced 25 response requests and 21 audio
  commits in about three minutes. Input transcription returned short word-like fragments such as
  `you`, so the consecutive-wordless counter reset and never tripped. The 30-minute ceiling was too
  slow to affect this session.

Source review found a separate stop-contract defect: production gives the session an `_EitherStop`
instance, but `_EitherStop` has no `set()` method. Both existing noise guards call `stop_event.set()`.
If either guard reached its threshold in production, it would raise instead of requesting a clean
sleep.

## Goal

Bound the cost of false VAD turns without asking a transcript model to decide whether speech was
real. On the fifth locally completed turn in any rolling 60-second window, Reachy must:

1. send no input commit or response request for that fifth turn;
2. end the Realtime session cleanly;
3. return to its sleeping pose; and
4. ignore wake words until an operator explicitly wakes or restarts the app.

The existing 30-minute awake-session ceiling remains an independent backstop for slower failures.

## Non-goals

- Distinguishing human intent from noise with text, audio classification, direction of arrival, or
  model judgment.
- Changing local VAD thresholds.
- Persisting the latch across an explicit app restart. Restarting the app is an operator rearm.
- Counting tool-output continuations as new user turns.
- Automatically restarting or rebooting either robot.

## Chosen design

### Fixed safety policy

The safety limits are named code constants:

- five locally completed turns;
- a rolling 60-second window; and
- a 30-minute session lifetime.

They have no environment-variable off switch. These are safety bounds, not deployment tuning.
Changing them requires a reviewed code change.

A locally completed turn is the local VAD `stopped` decision. It is counted before
`input_audio_buffer.commit()` and before `response.create()`. The wake greeting does not count as a
turn, so a tripped session can produce at most one greeting plus four user-turn responses within the
window.

### Rolling turn breaker

A small session component owns a deque of monotonic completion timestamps. For each locally
completed turn it:

1. removes timestamps whose age is 60 seconds or more, making the window `(now - 60s, now]`;
2. appends the current timestamp; and
3. reports a trip when the deque reaches five entries.

The deque survives Realtime socket reconnects because it belongs to the awake session, not a
connection epoch. Transcript content cannot reset it. Timestamps expire only with time.

When the fifth turn trips the breaker, the record loop emits `noise_bail.rate_limit`, marks the
session outcome as a noise bail, sets the session stop signal, and leaves the loop before any input
commit or response request. Normal teardown cancels connection tasks and returns a distinct
`SessionOutcome.NOISE_BAIL`.

### Stop-signal contract

`_EitherStop.set()` will set its session-local secondary event. Its primary app-stop event remains
read-only. This makes the combined signal obey the interface already assumed by the supervisor and
noise guard.

The session lifetime remains based on `RealtimeRobotSession.run()` start time, so reconnecting the
socket does not reset it. A lifetime trip follows the same `SessionOutcome.NOISE_BAIL` path and
latches sleep.

### Latched sleep

`PresenceManager` owns an in-memory `wake_latched` flag and reason. When a session returns
`NOISE_BAIL`, the manager sets the latch before finishing the AWAKE→SLEEPING transition.
The manager reads and changes the latch under its existing lifecycle lock. It sets the latch before
clearing the completed session's pending wake, so no worker callback can arm a new session in the
transition gap.

While latched:

- wake-word callbacks cannot arm a pending session;
- the robot stays in ordinary `SLEEPING` state and sleeping pose;
- `snapshot()` exposes `wake_latched: true` and its reason; and
- an ignored detection emits `wake.ignored` with reason `noise_bail_latched`.

An explicit dashboard/API manual wake clears the latch and starts one new session. A new app process
also starts unlatched. Manual sleep does not set the latch.

### Remove transcript-based protection

Remove the consecutive-wordless counter, its input-transcription event handling, and the
input-transcription session configuration. Remove the related environment settings rather than
keeping dead compatibility paths. The feature was introduced only for the failed guard, adds paid
transcription to every committed turn, and cannot enforce a spend bound.

The runtime may continue to expose `last_user`, but it will remain unset unless another independent
feature supplies user text later.

## Data flow

```text
local VAD stops a turn
        |
        v
record timestamp and prune rolling window
        |
        +-- fewer than 5 --------> commit input -> request response
        |
        `-- fifth within 60 s ---> record trip -> set session stop
                                      |
                                      v
                              NOISE_BAIL outcome
                                      |
                                      v
                         PresenceManager latches wake
                                      |
                                      v
                       sleeping; wake words are ignored
```

## Failure handling and observability

- `noise_bail.rate_limit` records the observed count and window, never transcript text or audio.
- `noise_bail.wall_clock` records session age and the fixed ceiling.
- `presence.noise_bail_latched` records the latch reason.
- Status reports the latch so the dashboard and `scripts/robot status` can distinguish ordinary
  wake-armed sleep from circuit-breaker sleep.
- A manual wake records that it cleared the latch.
- Connection errors keep their existing reconnect behavior and do not latch wake.

No room transcript or raw microphone audio is added to logs.

## Tests and acceptance

### Unit tests

- The breaker trips on the fifth timestamp inside 60 seconds.
- A timestamp at or beyond the expiry boundary leaves the rolling window.
- Transcript content is absent from the breaker API.
- Realtime reconnect reset does not clear recorded turn timestamps.
- `_EitherStop.set()` sets only the session-local event and makes `is_set()` true.

### Integration tests

- The real record-loop stop branch sends neither input commit nor `response.create()` for the fifth
  turn.
- `RealtimeRobotSession.run()` returns `NOISE_BAIL` after a rate or wall-clock trip.
- `PresenceManager` latches after `NOISE_BAIL`, ignores a wake-word callback, then clears the latch
  only for manual wake.
- Manual sleep and connection failure return to wake-armed sleep without a latch.
- Session configuration no longer requests paid input transcription.

### End-to-end hardware acceptance

Night Reachy stays off until the change passes the canonical local gate and code review. After an
explicit deploy approval:

1. start the app and verify ordinary sleeping is wake-armed;
2. wake manually in a controlled room;
3. generate five short VAD turns inside 60 seconds;
4. verify the fifth turn produces no response and Reachy returns to sleeping;
5. say the wake phrase and verify no session starts;
6. manually wake and verify one normal conversation can start; and
7. manually sleep, then stop the app again unless Doctor Biz asks to leave it running.

Acceptance requires the canonical repository checks, no new warnings, the structured trip and latch
events, and a direct status check showing no Realtime connection while latched.

## Alternatives rejected

**Classify short transcripts.** Whisper already produced plausible one-word fragments from noise.
Text rules can reduce false turns but cannot enforce a bound and add transcription cost.

**Auto-sleep and immediately rearm the wake detector.** This bounds one session but allows repeated
wake→five-turn→sleep cycles, so total spend remains unbounded.

**Use only a total session timeout.** A rapid loop can issue hundreds of responses before a long
timeout. The timeout remains useful only as a separate slow-failure backstop.

## Known tradeoff

A real fast conversation with five completed turns inside one minute will sleep early and require a
manual wake. Doctor Biz chose this spend-first bias. The failure is bounded, visible, and reversible;
the current runaway is not.
