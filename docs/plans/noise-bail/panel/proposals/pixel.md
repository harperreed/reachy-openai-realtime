# Pixel's proposal — noise bail via input transcription

**Persona:** LLM/transcript pragmatist. We already pay for world-class ASR. An empty transcript IS
the noise signal — don't hand-roll VAD.

---

## Slot 1 — Detection: enable input transcription, classify on the transcript

### The signal

Enable `input_audio_transcription` in the session config. The Realtime API emits
`conversation.item.input_audio_transcription.completed` after each committed turn. The transcript
field is either non-empty text or an empty string. That binary outcome — text vs. nothing — is the
noise classifier.

### Concrete change to `_session_config` in `realtime.py`

`RealtimeAudioConfigInputParam` gains one field:

```python
input=RealtimeAudioConfigInputParam(
    format=pcm24,
    noise_reduction={"type": "far_field"},
    turn_detection=None,
    transcription={"model": "whisper-1"},   # ADD THIS
),
```

The SDK type already accepts this (it maps to the live API parameter). No new model config key is
required for MVP; the model string can be promoted to `AppConfig` if we want operator control.

### Event handling in `_event_loop`

Add a branch for `conversation.item.input_audio_transcription.completed`:

```python
elif event_type == "conversation.item.input_audio_transcription.completed":
    transcript = (getattr(event, "transcript", "") or "").strip()
    self.status.record_transcript("user", transcript)
    self._on_user_transcript(transcript)
```

`_on_user_transcript` is a new private method that updates a noise counter and potentially triggers
the bail path (Slot 2).

### What counts as garbage

I define three tiers, evaluated in order:

1. **Empty** — `len(transcript) == 0` after strip. Definitive noise.
2. **Non-lexical** — transcript consists entirely of characters outside `\w` (Unicode word chars)
   plus spaces, after removing whitespace. Things like `"..."`, `"hmm"`, `"uh"`, and lone
   punctuation: classify as garbage. Concretely: `not re.search(r'\w', transcript, re.UNICODE)`.
3. **Sub-threshold length** — transcript is 1–3 characters and contains no vowel. This catches
   transcriptions like `"k"`, `"pf"`, `"tch"` that Whisper emits for short noise bursts. Threshold:
   `len(transcript) <= 3 and not re.search(r'[aeiouáéíóúàèìòùäëïöüāēīōū]', transcript, re.IGNORECASE)`.

Anything else — including `"yes"`, `"ok"`, `"mm-hmm"` — is treated as real speech. This is the
honest failure mode (see below).

### New state: `_noise_turn_count` (int, reset on real turn)

The counter lives on `RealtimeRobotSession`. It increments on garbage turns, resets to zero on any
real turn. This is the single source of truth for consecutive noise.

The counter resets on `reset_connection_state()` — no stale noise state crosses a reconnect.

---

## Slot 2 — Response: suppress reply, count, bail to sleep

### Per-turn: swallow the response request

The current code path (`decision.stopped` in `_record_loop`) unconditionally calls
`connection.response.create(...)`. The transcript event arrives asynchronously — after the buffer
commit — so we cannot gate `response.create` on it without restructuring the turn flow. Instead:

**The model still generates a response for garbage turns.** There is no clean way to cancel before
`response.create` without adding a waiting state and blocking the record loop. However, we can:

1. After `response.create`, when `_on_user_transcript` fires with a garbage result, cancel the
   active response via `connection.response.cancel(response_id=...)`.
2. Suppress the spoken output by adding the response ID to `_interrupted_response_ids` before
   audio arrives.

This wastes one round-trip of model inference per garbage turn. That is the honest cost of using
transcript-as-classifier without redesigning the turn flow. On the noise runaway scenario (dozens
of turns), this is still far cheaper than the current runaway.

### Alternative (cleaner but more invasive): deferred response.create

Hold off calling `response.create` after buffer commit until the transcript event arrives (with a
short timeout fallback). This requires a new `WAITING_TRANSCRIPT` FSM state and a timeout (e.g., 2s)
after which we assume real speech and proceed. It is the architecturally cleaner path but doubles
the latency of the first word of every response. I do not recommend it for MVP — silent cancel is
good enough and keeps the turn flow unchanged.

### After N garbage turns: bail to sleep

N = 3 (configurable via `AppConfig.noise_bail_turns: int = 3`, env
`REACHY_OPENAI_REALTIME_NOISE_BAIL_TURNS`).

When `_noise_turn_count >= noise_bail_turns`:

1. Reset the counter.
2. Set the stop event (`stop_event.set()`). The teardown fix (commit `3036352`) ensures this
   actually ends the session.
3. Log a structured event: `status.record_event("noise_bail.triggered", consecutive_turns=N)`.
4. Do NOT speak a goodbye. Silent bail is correct — if the room is noisy, playing audio just adds
   to the noise and confuses whoever finally addresses the robot.

Re-wake requires the wake word, exactly as designed.

---

## Why this approach

The energy VAD already committed the turn — audio is already sent to OpenAI. We are paying for
that inference whether we detect noise here or not. Whisper-1 runs in parallel with the model
response; on a bad turn its result arrives within ~500ms of the audio commit. Using that result to
classify is not adding a new signal processing layer — it is reading the receipt.

Bespoke frequency or energy analysis on the robot side will always lag OpenAI's ASR quality. Any
threshold we tune on today's ambient conditions will drift. The transcript does not drift.

---

## Cost and latency

**Added cost per turn:** Whisper-1 transcription. At OpenAI's current pricing, audio transcription
via Realtime API is billed as audio input tokens (same audio already being sent). There is no
separate transcription charge — the `transcription` field enables the ASR pass on audio already in
the input buffer. The marginal cost per turn is near zero; the audio was already billed.

**Added latency to real turns:** None. The transcript event arrives asynchronously; it does not
block `response.create`. The response generates in parallel. On garbage turns we do pay a
cancel-and-discard cost (~300–600ms to receive the cancel acknowledgement).

**Privacy:** No new boundary. Audio already streams to OpenAI per turn. The transcript is a
derivative of that audio, not additional data.

**Failure mode — real short utterance transcribes fine but the model still got noise audio:**
`"yes"` transcribes correctly. Whisper has no trouble with it. The model gets the audio and the
text. If the acoustic quality was bad enough that the model's response is incoherent, that's a
model problem, not a transcript-classifier problem. The transcript classifier's job is to catch the
case where *no human spoke* — which it does well. It does not guarantee the model understood the
human; it only guarantees we don't bail when a human spoke.

**Risk — Whisper transcribes loud noise as words:** Whisper will occasionally transcribe a bang or
a door slam as a word. In that case the classifier passes and the model gets a bad turn. This is
acceptable — the design bias says occasional bad turns are better than missing real speech. The
N-consecutive-garbage threshold means one such false-positive resets the counter; it does not
trigger bail.

**Risk — transcript event is delayed or missing:** If `conversation.item.input_audio_transcription.completed`
never arrives (network hiccup, API change), the cancel never fires and the response plays as usual.
This is a safe failure mode — the robot answers, which is the current behavior. A watchdog is not
needed here; silence is better than a spurious cancel.

---

## Interaction with bail-toward-sleep bias

The bias says: favor sleep, accept occasional deaf turns. This approach is aggressive:
- N=3 means three noise hits in a row → sleep, no warning.
- A real person who says one thing and gets ignored (genuine short-utterance misclassification) and
  then the room gets noisy will eventually bail the robot. That is acceptable by the bias statement.
- The counter resets on any real turn, so a mix of real and noisy turns never accumulates toward
  bail — only sustained noise runs.

---

## How I'd test it (TDD order)

**Test 1 (failing first):** `test_garbage_classifier` — unit test on the `_is_garbage_transcript`
function (extracted as a pure function): empty string → True, `"..."` → True, `"k"` → True,
`"yes"` → False, `"ok"` → False, `"mm-hmm"` → False, `"助けて"` → False (Japanese is real).

**Test 2:** `test_noise_counter_increments_on_garbage` — given a mock session, fire
`_on_user_transcript("")` three times, assert `_noise_turn_count == 3`.

**Test 3:** `test_noise_counter_resets_on_real_turn` — fire garbage twice, then
`_on_user_transcript("hello")`, assert counter is 0.

**Test 4:** `test_bail_triggers_stop_event` — fire N garbage turns, assert stop_event was set.

**Test 5:** `test_response_cancelled_on_garbage` — integration-level: fake connection, commit a
turn, transcript event arrives as garbage, assert `response.cancel` was called with the active
response ID.

These are all unit/integration tests on the session class with a fake connection. No mocks testing
mocks — the fake connection is a stub that records calls, not a mock of internal behavior.
