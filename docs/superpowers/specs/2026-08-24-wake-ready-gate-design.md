<!-- ABOUTME: Design for a two-step wake flow that discards cold-start audio until Reachy is ready. -->
<!-- ABOUTME: A local beep marks the exact point when microphone input may enter the Realtime session. -->

# Wake Ready Gate Design

**Status:** Approved by Doctor Biz on 2026-08-24.

## Incident

Reachy's speech understanding regressed after the wake-word path was added. A controlled night-robot
test isolated the fault to wake-enabled startup: the same build worked well when wake mode was
disabled, while wake mode took about 4.6 seconds to connect and requested its first response about
0.16 seconds after the session became ready. Code inspection confirms that wake mode seeds captured
pre-roll and buffers the live microphone during this gap. The timing is consistent with that queued
audio entering the new session immediately rather than as a clean post-ready turn.

The current wake flow was designed to support a single utterance such as "Hey Reachy, ..." by
buffering audio across wake detection and connection startup. In practice, the Realtime session can
receive the wake phrase, room noise, partial speech, and several seconds of queued audio at once.
That makes the first turn hard to segment and can trigger extra commits or responses.

The always-connected comparison establishes that the microphone, model, base VAD, and playback path
can support a good conversation. This change therefore targets the wake-to-session handoff rather
than retuning those working parts.

## Goal

Make wake-enabled operation a clear two-step interaction:

1. Doctor Biz says "Hey Reachy."
2. Reachy connects to OpenAI while discarding microphone audio.
3. Reachy plays one short local ready beep.
4. Reachy accepts the request spoken after the beep.

The beep is the contract: before it, the application rejects Realtime input; after it, the
application accepts input.

## Success criteria

- A wake phrase alone sends no input commit and requests no response before the ready beep.
- All microphone frames captured before beep completion are discarded, not replayed later.
- Each successful wake plays exactly one beep.
- The first request after the beep becomes one clean user turn and one response.
- Doctor Biz judges the post-beep understanding as good as the successful wake-disabled comparison.
- Manual wake follows the same ready-gated flow.
- Wake-disabled behavior and its startup greeting remain unchanged.
- Existing circuit-breaker, session-ceiling, wake-latch, and sleep behavior remain intact.

## Non-goals

- Supporting "Hey Reachy, <request>" as one continuous utterance.
- Buffering or recovering speech spoken before the ready beep.
- Keeping a Realtime session connected while Reachy sleeps.
- Retuning VAD, microphone, playback freshness, prompts, or the Realtime model.
- Solving self-interruption as a separate problem.
- Removing the existing wake pre-roll code or public settings in this change.
- Adding user-configurable beep settings.

## Chosen design

### Readiness gate

The wake-enabled session has one readiness gate controlling whether captured microphone frames may
reach OpenAI. The gate starts closed and remains closed through wake detection, Realtime connection,
session configuration, and ready-beep playback.

Capture continues draining the shared microphone pipeline while the gate is closed. The wake flow
must discard those frames downstream; it must never pause the capture worker or create a second
microphone reader. No pre-roll or connection-time audio is appended to the Realtime input buffer.

After the session reports that its configuration is ready, the wake flow queues the local beep on
the existing speaker worker and waits for the write acknowledgement, tone duration, and output
guard defined below. It then clears pending local speech detection and pre-roll state before opening
the readiness gate. Only microphone frames captured after that point may enter normal VAD and
Realtime input handling.

The readiness gate is the single source of truth for input acceptance. Sleep, stop, or startup
cancellation closes it immediately.

### Presence and session lifecycle

`WAKING` covers connection and beep playback. The presence manager must not transition to `AWAKE`
until the beep has finished and the readiness gate is open. Status therefore cannot claim that
Reachy is listening during the cold-start gap.

Wake-word and manual dashboard activation use the same startup path. The presence manager marks both
with an explicit internal `wake_session` flag when it builds the session. This flag distinguishes a
manual wake from the wake-disabled supervisor; the presence or absence of buffered audio must not
decide whether a session greets or opens its input.

Wake-enabled sessions do not request a model greeting; the local beep is their only ready
acknowledgement. Wake-disabled mode keeps its existing always-connected startup and greeting
behavior.

The existing wake pre-roll buffer and configuration remain in place because safe pre-beep buffering
may be reconsidered later. This change stops consuming that buffer for Realtime startup. It does not
add a second compatibility path: wake-enabled input has one rule, which is to start after the beep.

### Ready beep

The application generates a mono float32 sine tone locally at the robot's current output sample rate
with these fixed initial values:

- frequency: 880 Hz;
- duration: 160 ms; and
- amplitude: 0.15 on the float32 `[-1.0, 1.0]` scale.

The implementation submits the tone directly to the current speaker worker, not the response jitter
buffer. That queue item carries a completion result. The worker reports success only after
`push_audio_sample` returns; a write exception reports failure. Because the Reachy SDK does not
provide a physical-playback completion callback, the wake flow then waits until at least the tone's
160 ms duration plus a fixed 100 ms output guard has elapsed since submission before opening input.
The existing 10-second wake startup deadline bounds connection, write acknowledgement, and this
guard together.

The implementation must not call a separate SDK sound helper: Reachy Mini Wireless shares one
GStreamer pipeline for microphone and speaker, and competing playback control can stall capture.

The constants stay internal. They can become settings only after a real need appears.

## Data flow

```text
wake word or manual wake
          |
          v
      WAKING; gate closed
          |
          +---- capture worker keeps draining ----> discard frames
          |
          v
  Realtime session configured
          |
          v
 queue local beep on speaker worker
          |
          v
 wait for speaker acknowledgement and output guard
          |
          v
 clear VAD and pre-roll state
          |
          v
 open input gate; transition AWAKE
          |
          v
 post-beep speech -> VAD -> commit -> response
```

## Failure handling

- If connection or session configuration fails before the gate first opens, Reachy keeps the gate
  closed and ends that wake attempt instead of entering the normal awake reconnect loop. Reachy then
  returns to sleeping through the existing startup-failure path.
- If beep enqueue or speaker write acknowledgement fails before the existing 10-second wake startup
  deadline, Reachy does not silently claim readiness. It records the failure, closes the session,
  and returns to sleeping.
- If sleep, app stop, or disconnect occurs during connection or beep playback, startup cancels,
  the gate stays closed, and no late completion may reopen it. A completion is valid only while its
  connection epoch is still current and no stop signal is set.
- Reconnect behavior after an already-awake connection loss remains unchanged. The ready beep marks
  wake-session startup, not each socket reconnect.

## Observability and privacy

Structured events record:

- `wake.ready_beep_started` with no audio payload;
- `wake.ready_beep_completed` with startup duration and the aggregate count of discarded frames;
- `wake.input_gate_opened` after the output guard; and
- the existing startup-failure event with a safe failure-stage field.

Events may contain monotonic durations, counts, and safe state names. They must not contain raw audio
or room transcripts. Discard accounting is aggregated at beep completion rather than logged once per
frame.

## Tests and acceptance

### Unit tests

- A new wake session starts with the readiness gate closed.
- Frames received while the gate is closed never reach VAD or Realtime input handling.
- The gate opens only after a successful speaker write acknowledgement and the output guard.
- Stop, sleep, failure, and cancellation keep or return the gate to closed.
- Late beep completion after cancellation cannot reopen the gate.
- The generated float32 samples have the expected channel shape, duration, frequency, and amplitude
  bounds at more than one output sample rate.

### Integration tests

- The real capture subscription, session coordination, and playback queue discard generated
  pre-beep PCM and accept generated post-beep PCM in order.
- Session readiness queues exactly one beep and waits for a successful worker write acknowledgement
  plus the output guard before accepting input.
- A disconnect during startup returns to sleeping; an acknowledgement from that stale connection
  epoch cannot open the gate or cause an awake transition.
- Wake-enabled startup sends no model greeting, input commit, or response request before the gate
  opens.
- Manual wake and wake-word activation use the same ready-gated session path.
- Wake-disabled startup keeps its current behavior.
- Existing noise breaker and session-stop paths still end the session without a late gate reopen.

Tests exercise state and data flow through the real application components. They do not assert that
mock objects received expected calls.

### End-to-end hardware acceptance

After the canonical local checks and code review pass, and with Doctor Biz's approval to use the
night robot at `192.168.200.128`:

1. start the app in wake-enabled mode and verify it is sleeping and disconnected;
2. say only the wake phrase;
3. verify zero input commits and zero response requests before one ready beep;
4. during a separate wake, speak before the beep and verify that speech is discarded;
5. after the beep, speak one fixed test phrase;
6. verify exactly one input commit and one response, then have Doctor Biz judge understanding;
7. verify manual sleep and manual wake use the same gate and beep; and
8. stop the app when testing ends.

Acceptance requires the canonical command `uv run ruff check . && uv run pytest`, no new warnings,
the safe structured events above, and a direct status check confirming no Realtime connection after
the final app stop.

## Alternatives rejected

**Keep buffering through cold connection.** This preserves one-shot commands but caused the
regression under test. Buffer selection and turn-boundary recovery add risk that is not needed for
the approved two-step interaction.

**Keep a Realtime session connected while sleeping.** This removes connection delay but restores
idle session cost and changes the privacy and lifecycle contract of wake mode.

**Return to always-connected mode.** The controlled comparison proves that path works, but disabling
wake mode would remove the requested local wake behavior instead of fixing its handoff.

## Known tradeoff

Doctor Biz must wait for the beep and repeat any request spoken too early. That is deliberate for the
first version: a visible, testable input boundary is simpler and safer than guessing which startup
audio belongs to the request. Pre-beep buffering can be designed later if this interaction proves
too awkward.

The SDK cannot confirm the instant a tone reaches the physical speaker. The worker acknowledgement,
tone duration, and 100 ms guard form a conservative local estimate. Hardware acceptance must confirm
that the microphone gate opens after the audible beep; if it does not, this design is not accepted.
