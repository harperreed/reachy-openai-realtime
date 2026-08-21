<!-- ABOUTME: Phase 2A "Hey Reachy" wake word spec — verbatim copy of GitHub issue #12 body -->
<!-- ABOUTME: plus a repo addendum recording decisions resolved with Harper on 2026-08-21. -->

> Provenance: sections 1-39 below are the body of
> https://github.com/tinjyuu/reachy-openai-realtime/issues/12, copied verbatim on 2026-08-21.
> The addendum at the end is repo-local and travels with this file.

# Reachy Phase 2A — “Hey Reachy” Wake Word Integration Spec

## 1. Goal

Add a reliable local **“Hey Reachy”** wake-word mode to the existing Reachy OpenAI Realtime application.

Use the already-trained Edge Impulse model from:

```text
luisomoreau/hey_reachy_wake_word_detection
```

Specifically, use the provided Linux AARCH64 `.eim` model suitable for Reachy Mini Wireless.

Do **not**:

* train a new wake-word model
* use Picovoice
* add a second microphone capture path
* introduce external brain, memory, push events, or idle timeout in this phase

This phase should be narrowly scoped to:

```text
BOOT
  ↓
SLEEPING
  ↓
"Hey Reachy"
  ↓
WAKING
  ↓
OpenAI Realtime connected
  ↓
AWAKE
```

---

# 2. Desired user experience

At boot:

```text
Reachy starts
    ↓
small local boot movement
    ↓
enters sleeping pose
    ↓
local wake-word detector listens
```

While sleeping:

* no OpenAI Realtime connection is required
* audio stays local
* no sleeping-room audio is sent to OpenAI
* camera AI input is disabled
* wake detector continuously listens for `"Hey Reachy"`

User says:

```text
"Hey Reachy, what's the weather today?"
```

Reachy should:

1. detect `"Hey Reachy"` locally
2. immediately perform a small wake acknowledgement motion
3. start OpenAI Realtime connection concurrently
4. continue buffering the rest of the user's sentence
5. submit the post-wake speech once Realtime is ready
6. answer without asking the user to repeat the question

This must also work:

```text
User: "Hey Reachy"

[pause]

User: "What's the weather?"
```

---

# 3. Existing wake-word model

Use the existing trained Edge Impulse model from the Hugging Face Reachy wake-word project.

Expected model:

```text
hey-reachy-wake-word-detection-linux-aarch64.eim
```

The reference project uses:

```text
sample rate:       24 kHz
window size:       ~12,000 samples / ~500 ms
classes:
    hey_reachy
    noise
    other
```

Initial detection threshold:

```text
0.70
```

Make the threshold configurable.

Do not copy the reference project's microphone architecture directly.

The reference implementation opened audio independently. Our application already has a hardened/shared audio architecture and must retain **one microphone owner**.

---

# 4. Critical architecture rule

There must be exactly one Reachy microphone capture pipeline.

Existing architecture:

```text
Reachy ReSpeaker
      ↓
AudioCaptureWorker
```

Extend that into:

```text
                    AudioCaptureWorker
                           │
           ┌───────────────┴────────────────┐
           │                                │
           ▼                                ▼
   WakeWordConsumer                  RealtimeConsumer
   active while sleeping             active while awake
```

Do not do:

```text
Reachy media API → wake-word process

AND

Reachy media API → Realtime process
```

Do not create:

* another `sounddevice` stream
* another ALSA reader
* another GStreamer pipeline

Wake-word inference consumes copies of frames produced by the existing `AudioCaptureWorker`.

---

# 5. Presence state machine

Add a separate high-level presence state.

```python
from enum import Enum, auto


class PresenceState(Enum):
    BOOTING = auto()
    SLEEPING = auto()
    WAKING = auto()
    AWAKE = auto()
    ERROR = auto()
```

State transitions:

```text
BOOTING
    ↓
SLEEPING

SLEEPING
    ↓ wake word
WAKING

WAKING
    ↓ Realtime ready
AWAKE

WAKING
    ↓ startup failure
SLEEPING
```

Manual development controls may allow:

```text
SLEEPING → WAKING
AWAKE → SLEEPING
```

Do not implement automatic idle sleep yet.

---

# 6. Wake-word detector abstraction

Create a provider abstraction even though only Edge Impulse is initially implemented.

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WakeWordDetection:
    phrase: str
    score: float
    detected_at: float


class WakeWordDetector(Protocol):
    @property
    def required_sample_rate(self) -> int:
        ...

    def start(self) -> None:
        ...

    def process(self, pcm16: bytes) -> WakeWordDetection | None:
        ...

    def reset(self) -> None:
        ...

    def close(self) -> None:
        ...
```

Initial implementation:

```text
EdgeImpulseWakeWordDetector
```

Do not put presence-state or OpenAI logic inside the detector.

---

# 7. Edge Impulse adapter

Create:

```text
wakeword/edge_impulse.py
```

Responsibilities:

* locate/load the `.eim`
* initialize the Edge Impulse classifier
* consume PCM16 audio
* invoke streaming classification
* extract `hey_reachy` confidence
* compare against configured threshold
* return `WakeWordDetection`
* handle classifier errors without crashing the app

Do not allow the Edge Impulse runtime to own microphone capture.

Conceptually:

```python
class EdgeImpulseWakeWordDetector:
    def __init__(
        self,
        model_path: str,
        threshold: float = 0.70,
    ):
        ...

    @property
    def required_sample_rate(self) -> int:
        return 24_000

    def process(self, pcm16: bytes) -> WakeWordDetection | None:
        scores = self.classifier.classify(pcm16)

        score = scores["hey_reachy"]

        if score >= self.threshold:
            return WakeWordDetection(
                phrase="hey reachy",
                score=score,
                detected_at=time.monotonic(),
            )

        return None
```

Adapt to the actual Edge Impulse Python/Linux runner API rather than assuming the exact pseudo-interface above.

---

# 8. Model packaging

Preferred development behavior:

```text
models/
    hey-reachy-wake-word-detection-linux-aarch64.eim
```

Configuration should permit overriding the path:

```json
{
  "wake_word": {
    "model_path": "/path/to/model.eim"
  }
}
```

If the model cannot be loaded:

* record a clear error
* mark wake subsystem unhealthy
* keep the management UI running
* allow manual wake
* do not crash-loop the entire application

### Licensing note

Before redistributing the `.eim` as part of a commercial/product release, confirm redistribution/license permission from the original model author.

Until that is clarified, support:

```text
external/local model path
```

so development does not depend on bundling the artifact.

---

# 9. Audio fan-out

If not already present from Phase 1, add safe fan-out from `AudioCaptureWorker`.

Suggested frame representation:

```python
@dataclass(frozen=True)
class AudioFrame:
    samples: np.ndarray
    sample_rate: int
    captured_at: float
```

Logical consumers:

```text
WakeWordConsumer
RealtimeConsumer
DiagnosticsConsumer
```

Each consumer has its own bounded queue.

Wake queue target:

```text
<= 500 ms buffered
```

If wake inference falls behind:

```text
drop oldest frames
```

Never block microphone capture.

---

# 10. Audio preprocessing

Wake-word model expects:

```text
24 kHz
mono
PCM16
```

Create one shared converter:

```python
def prepare_wake_audio(
    frame: AudioFrame,
    target_rate: int = 24_000,
) -> np.ndarray:
    ...
```

Responsibilities:

1. select mono channel
2. resample if necessary
3. clamp samples
4. convert to signed int16
5. accumulate model-compatible windows

Do not duplicate this code inside the Edge Impulse adapter.

---

# 11. Wake worker

Wake inference should run independently of the asyncio Realtime loop.

Suggested architecture:

```text
AudioCaptureWorker
       ↓
bounded wake queue
       ↓
WakeWordWorker
       ↓
EdgeImpulseWakeWordDetector
       ↓
WakeEvent
```

`WakeWordWorker` must:

* run continuously while `SLEEPING`
* process frames sequentially
* maintain heartbeat timestamp
* catch detector exceptions
* never block audio capture
* emit wake events through a thread-safe/async-safe queue

Track:

```text
last_frame_processed_at
frames_processed
detections
errors
```

---

# 12. Wake debounce

The model may produce positive detections across multiple overlapping windows.

Add application-level debounce.

Default:

```python
WAKE_DEBOUNCE_SECONDS = 2.0
```

Once a wake has been accepted:

```text
disable wake processing
```

until Reachy returns to `SLEEPING`.

A single phrase must never produce multiple concurrent Realtime connections.

---

# 13. Sleep audio ring buffer

While sleeping, maintain an in-memory rolling audio buffer.

Purpose:

```text
"Hey Reachy, tell me a joke."
```

The portion after `"Hey Reachy"` must not disappear while Realtime is connecting.

Recommended:

```text
sleep audio history: 4 seconds
```

Implementation:

```python
class AudioRingBuffer:
    def append(self, frame: AudioFrame) -> None:
        ...

    def since(self, timestamp: float) -> list[AudioFrame]:
        ...

    def clear(self) -> None:
        ...
```

Requirements:

* bounded by time, not unlimited list growth
* memory-only
* never persisted
* lock/thread safe

---

# 14. Wake event

When `"Hey Reachy"` fires:

```python
@dataclass(frozen=True)
class WakeEvent:
    id: str
    detected_at: float
    phrase: str
    score: float
```

The wake handler must process each wake-event ID at most once.

---

# 15. Immediate local wake acknowledgement

Wake acknowledgement must not wait for:

* OpenAI
* DNS
* internet
* Realtime WebSocket
* speech generation

Immediately after a wake event:

```text
SLEEPING
   ↓
WAKING
```

then perform a small deterministic local motion through `MotionManager`.

Suggested:

```text
slight head raise
small antenna perk
brief listening nod
```

Target:

```text
wake detection → visible acknowledgement
< 200 ms where practical
```

This should be a built-in/local motion, not an LLM-selected Hugging Face emote.

---

# 16. OpenAI connection startup

At the same time as local acknowledgement:

```python
asyncio.create_task(
    realtime_session_manager.start_from_wake(...)
)
```

Do not block:

* audio capture
* wake worker
* motion acknowledgement

Expected timeline:

```text
T+0       wake detected
T+50ms    local motion starts
T+50ms    Realtime connection starts
T+...     microphone keeps buffering
T+N       session.updated received
T+N       buffered post-wake audio flushed
T+N       normal conversation begins
```

---

# 17. Preserve continuous utterance

This is a required feature.

Must support:

```text
"Hey Reachy, tell me about Chicago."
```

without requiring a pause or repetition.

During `WAKING`, continue buffering microphone input.

The assembled wake audio should include:

```text
small pre-roll
+
audio immediately after wake detection
+
audio captured while Realtime connects
```

Do not replay the full four-second sleeping buffer.

---

# 18. WakeAudioAssembler

Create:

```python
class WakeAudioAssembler:
    def assemble(
        self,
        history: AudioRingBuffer,
        wake: WakeEvent,
        waking_audio: list[AudioFrame],
    ) -> list[AudioFrame]:
        ...
```

Initial strategy should be conservative.

Use approximately:

```text
250–500 ms before wake-detection timestamp
+
all subsequent audio
```

This may include the tail end of `"Reachy"`.

That is acceptable.

Do not spend significant engineering effort precisely locating/removing the wake phrase unless Edge Impulse provides a reliable phrase boundary.

The OpenAI model can tolerate hearing:

```text
"...Reachy, what's the weather?"
```

much better than the user tolerates repeating themselves.

---

# 19. Maximum wake buffer

Realtime connection failure must not cause unbounded buffering.

Default:

```python
MAX_WAKE_BUFFER_SECONDS = 10
```

When exceeded:

1. stop adding wake-session audio
2. cancel/bound connection attempt
3. log failure
4. perform local failure indication
5. return to `SLEEPING`

Do not retain the buffered audio after failure.

---

# 20. Failed wake behavior

Example:

```text
User: "Hey Reachy"

Internet unavailable.
```

Required behavior:

```text
wake detected
    ↓
Reachy perks up locally
    ↓
Realtime connection attempted
    ↓
bounded failure
    ↓
small local "couldn't connect" movement
    ↓
return to SLEEPING
```

Do not:

* remain awake forever
* continuously retry while sleeping
* reboot Reachy
* crash the whole application

---

# 21. Boot behavior

After Phase-1 subsystems initialize:

```text
BOOTING
   ↓
optional short boot motion
   ↓
initialize wake model
   ↓
SLEEPING
```

Boot animation:

* local
* under ~2 seconds
* optional/configurable
* runs through `MotionManager`

If wake model initialization fails:

```text
PresenceState.ERROR
```

but keep:

* UI
* diagnostics
* manual wake

available.

---

# 22. Sleeping pose

Define a clear but subtle local sleeping pose.

Suggested:

```text
head slightly lowered
antennas relaxed
minimal/no speaking animation
```

Avoid constant large movements while asleep.

Wake acknowledgement should visually contrast with sleeping pose.

---

# 23. Wake detector watchdog

Track:

```text
wake_worker_heartbeat_at
wake_frames_processed
wake_worker_restart_count
wake_backend_error_count
```

While sleeping, if no wake frames are processed for approximately:

```text
2 seconds
```

and the audio capture worker is healthy:

```text
restart WakeWordWorker
```

If the shared microphone itself is stalled:

```text
Phase-1 audio watchdog owns recovery
```

The wake subsystem must **not** independently restart the media pipeline.

Responsibility boundaries:

```text
AudioCaptureWorker
    microphone/media health

WakeWordWorker
    classifier health

PresenceManager
    sleep/wake behavior
```

---

# 24. Manual development controls

Add:

```http
POST /api/presence/wake
POST /api/presence/sleep
```

Manual wake:

```text
SLEEPING → WAKING → AWAKE
```

without wake audio replay.

Manual sleep:

```text
AWAKE → SLEEPING
```

for development/testing only.

Automatic idle timeout will be implemented separately.

---

# 25. Settings

Suggested configuration:

```json
{
  "wake_word": {
    "enabled": true,
    "backend": "edge_impulse",
    "phrase": "hey reachy",
    "model_path": "models/hey-reachy-wake-word-detection-linux-aarch64.eim",
    "threshold": 0.70,
    "debounce_seconds": 2.0,
    "history_seconds": 4.0,
    "wake_preroll_ms": 400,
    "max_wake_buffer_seconds": 10,
    "wake_motion_enabled": true,
    "boot_motion_enabled": true
  }
}
```

Validate:

```text
0.0 < threshold <= 1.0

0.5 <= debounce_seconds <= 10

1 <= history_seconds <= 10

100 <= wake_preroll_ms <= 1000

2 <= max_wake_buffer_seconds <= 30
```

---

# 26. UI

Add a small **Wake Word** panel.

```text
WAKE WORD

Enabled                      [✓]

Backend
Edge Impulse

Phrase
Hey Reachy

Detection threshold
[────────●────] 0.70

Model
hey-reachy-wake-word-detection-linux-aarch64.eim

Status
● Ready

Presence
SLEEPING

Frames processed
145,832

Last detection
9:42:18 PM

Last score
0.91

[ Wake Reachy ]
[ Sleep Reachy ]
```

Optional development-only view:

```text
current hey_reachy score
current noise score
current other score
```

Do not expose raw microphone audio in the UI.

---

# 27. Structured logging

Add:

```text
presence.booting
presence.sleeping
presence.waking
presence.awake
presence.error

wake.model_loading
wake.model_ready
wake.model_error

wake.detected
wake.debounced

wake.connection_start
wake.session_ready
wake.connection_failed

wake.buffer_started
wake.buffer_flushed
wake.buffer_overflow

wake.worker_stalled
wake.worker_restarted

wake.manual
```

Example:

```json
{
  "timestamp": "2026-08-17T21:44:13.488-05:00",
  "event": "wake.detected",
  "presence_state": "SLEEPING",
  "backend": "edge_impulse",
  "phrase": "hey reachy",
  "score": 0.91
}
```

Never log sleeping audio.

---

# 28. Metrics

Track:

```text
wake_count

wake_score

wake_detect_to_motion_ms

wake_detect_to_connection_start_ms

wake_detect_to_session_ready_ms

wake_detect_to_first_response_audio_ms

wake_connection_failure_count

wake_worker_restart_count

wake_buffer_peak_ms
```

Expose rolling measurements through existing diagnostics.

---

# 29. False-positive diagnostics

For initial physical testing, store only metadata around detections:

```text
timestamp
score
state
whether subsequent user speech occurred
```

Do not automatically store audio.

Add an optional development setting:

```text
debug_capture_wake_audio=false
```

If enabled manually, permit short wake-event clips to be saved for debugging.

It must default to `false`.

This can help tune the threshold without turning the robot into an always-on recorder.

---

# 30. Detection tuning

Start at:

```text
threshold = 0.70
```

Test against:

### Positive

```text
Hey Reachy
hey reachy
HEY REACHY
Hey Reachy, what's up?
Hey Reachy can you look at me?
```

### Hard negatives

```text
Hey Rachel
Hey Reggie
Hey Richie
Hey really
Reachy
Hey Siri
```

### Environmental

```text
quiet room
office conversation
music
TV
podcast
Reachy's own speaker
near-field speaker
far-field speaker
multiple accents
```

Tune the threshold on the actual Reachy Mini Wireless microphone.

Do not assume the reference project's threshold is optimal for our acoustic environment.

---

# 31. Realtime interaction while asleep

When `PresenceState.SLEEPING`:

```text
Realtime connection:
    absent/disconnected

Realtime audio consumer:
    disabled

wake-word consumer:
    enabled
```

When `PresenceState.WAKING`:

```text
wake detector:
    disabled/debounced

Realtime:
    connecting

audio:
    buffering locally
```

When `PresenceState.AWAKE`:

```text
Realtime:
    active

Realtime audio consumer:
    active

wake detector:
    inactive
```

Do not run normal wake detection while Reachy is already awake.

---

# 32. Resource cleanup

Transitions must not leak workers.

On entering `SLEEPING`:

```text
ensure no Realtime session remains
reset detector
clear wake buffer
enable wake consumer
```

On entering `WAKING`:

```text
disable additional wake triggers
start wake audio buffer
```

On entering `AWAKE`:

```text
flush/clear wake buffer
leave wake detector dormant
```

After failure:

```text
close failed Realtime session
clear wake audio
reset wake detector
return SLEEPING
```

---

# 33. Tests

Add:

```text
tests/test_wakeword_edge_impulse.py
tests/test_wakeword_worker.py
tests/test_wake_audio_buffer.py
tests/test_presence_manager.py
tests/test_wake_realtime_integration.py
tests/test_wake_recovery.py
```

Mock Edge Impulse output where possible.

Do not require physical hardware or the real `.eim` for all unit tests.

---

# 34. Required test cases

### State

```text
BOOTING → SLEEPING

SLEEPING + wake → WAKING

WAKING + session ready → AWAKE

WAKING + connection failure → SLEEPING
```

### Detection

```text
score .69 with threshold .70 → no wake

score .70 → wake

score .90 → wake

multiple detections during debounce → one wake
```

### Audio

```text
wake detector consumes shared mic frames

wake detector never owns microphone

ring buffer remains bounded

wake buffer remains bounded

oldest wake frames drop under overload
```

### Continuous command

Simulate:

```text
"Hey Reachy, tell me a joke"
```

and verify post-wake speech is preserved and sent after session initialization.

### Recovery

```text
Edge Impulse classifier throws

wake worker stalls

Realtime startup fails

Realtime startup hangs

microphone restarts

100 sleep/wake cycles
```

Verify:

```text
one audio capture worker
one wake worker
no duplicate Realtime connections
bounded threads/tasks
bounded audio buffers
```

---

# 35. Physical acceptance criteria

The phase is complete when all of these work on a Reachy Mini Wireless.

### Wake

From sleeping:

```text
"Hey Reachy"
```

reliably produces immediate visible acknowledgement.

### Continuous speech

```text
"Hey Reachy, what time is it?"
```

works without repeating the question.

### Privacy

Before wake detection:

```text
0 microphone frames transmitted to OpenAI
```

### Offline behavior

With internet disconnected:

```text
"Hey Reachy"
```

still triggers local acknowledgement, then fails gracefully back to sleep.

### Recovery

Repeated wake cycles do not require restarting:

```text
Reachy
GStreamer
wake detector
application
```

### Stability

Run at least:

```text
100 wake → awake → sleep cycles
```

with no growing worker/thread/task counts.

---

# 36. Suggested module layout

```text
reachy_openai_realtime/

    presence/
        __init__.py
        manager.py
        states.py

    wakeword/
        __init__.py
        base.py
        edge_impulse.py
        worker.py
        buffer.py

    audio/
        capture.py
        fanout.py
```

Do not create extra abstractions purely for architectural aesthetics. Reuse Phase-1 components where they already satisfy the requirement.

---

# 37. Implementation order

## Step 1

Confirm the existing `AudioCaptureWorker` can fan out frames safely.

If necessary, implement bounded subscriber queues.

## Step 2

Run the existing `.eim` manually on Reachy ARM64 and prove:

```text
audio frame → classifier → hey_reachy score
```

before doing any presence integration.

## Step 3

Implement:

```text
EdgeImpulseWakeWordDetector
WakeWordWorker
```

## Step 4

Implement:

```text
BOOTING
SLEEPING
WAKING
AWAKE
```

presence states.

## Step 5

Wake event → immediate local MotionManager acknowledgement.

## Step 6

Wake event → async Realtime startup.

## Step 7

Implement sleep/waking audio ring buffer.

## Step 8

Make:

```text
"Hey Reachy, <question>"
```

work continuously.

## Step 9

Add watchdogs, diagnostics, settings and logging.

## Step 10

Tune threshold on physical Reachy.

---

# 38. Implementation constraints for Claude/Codex

* Use the existing Luis Moreau Edge Impulse `"Hey Reachy"` model.
* Do not train a new model.
* Do not introduce Picovoice.
* Do not introduce sherpa-onnx.
* Do not introduce openWakeWord unless requested separately.
* Do not create a second microphone reader.
* Do not modify Phase-1 Realtime architecture unnecessarily.
* Wake inference must never block microphone capture.
* Sleeping audio must remain local.
* Raw sleeping audio must not be persisted by default.
* Wake connection attempts must be bounded.
* All queues/buffers must be bounded.
* All sleep/wake transitions must be idempotent.
* Use existing Phase-1 logging, watchdog, FSM, and MotionManager conventions where appropriate.
* Do not implement idle timeout, memory, external brain, or push events in this PR.
* Add tests alongside implementation.

---

# 39. Definition of done

The result should behave like:

```text
Reachy boots.

*small boot animation*

Reachy settles into sleep.

No OpenAI connection is active.

The existing microphone worker continues capturing locally.
Audio is copied to the Edge Impulse wake detector.

              ↓

"Hey Reachy, what's your favorite movie?"

              ↓

hey_reachy score crosses threshold.

              ↓

Reachy immediately perks up.

At the same time:

    OpenAI Realtime begins connecting.

The rest of the user's sentence continues being buffered.

              ↓

Realtime becomes ready.

              ↓

Buffered question audio is submitted.

              ↓

Reachy answers.

The user never has to repeat the question.
```

The core rule for this implementation is:

> **Use the wake-word model we already have, keep one microphone pipeline, acknowledge locally, and hide Realtime startup latency by buffering the user's speech.**


---

# Addendum: repo resolutions (2026-08-21, Harper + Claude)

Decisions taken against this spec before planning. Where the spec offers latitude, these bind.

## A1. Edge Impulse runtime: vendored .eim client, no new dependencies

The `edge-impulse-linux` PyPI package is NOT added. Its package `__init__` unconditionally
imports its audio module, which imports PyAudio (native, source-built on the robot) and six —
all dead weight for us, since the one-mic-owner rule (spec section 9) forbids its
`AudioImpulseRunner` anyway. Instead `wakeword/eim_runner.py` implements the .eim runner
protocol directly (~150 lines, stdlib + numpy only): spawn the executable model with a Unix
socket path argument, exchange JSON (`{"hello": 1}` handshake returning `model_parameters`
including `frequency` and `slice_size`; `{"classify": [samples...], "id": N}`; responses are
`\x00`-terminated). Protocol verified against the open-source SDK
(edgeimpulse/linux-sdk-python `runner.py`, v1.2.2). The optional shared-memory fast path is
omitted: at one ~500 ms window every ~250 ms, plain JSON is far below any relevant latency floor.
The .eim file must be chmod +x before spawn (the runner IS the model binary).

## A2. Model acquisition: auto-download from the author's HF Space, never bundled

The model stays out of this repo and its HF Space (no redistribution license). On first need the
app downloads `hey_reachy_wake_word_detection/models/hey-reachy-wake-word-detection-linux-aarch64.eim`
from https://huggingface.co/spaces/luisomoreau/hey_reachy_wake_word_detection at a PINNED revision
with a recorded sha256, into `$CONFIG_DIR/models/`. Pinned values (verified 2026-08-21):
revision `3b6670748dc3ffda9f09dce18810b283ace7147e`, aarch64 file size 13,574,768 bytes, sha256
`9861b8d43bd9a2b95bf0105262d358c9f6b5aa17fa0b266b0dadae8328c3f229` (ELF 64-bit ARM aarch64
executable — the model IS the runner binary). `wake_word.model_path` overrides for any local
file (dev boxes use the mac/x86_64 builds from the same Space). Download failure or checksum
mismatch -> `PresenceState.ERROR` per section 21, UI keeps running, no crash loop.

## A3. Capture rate reality

The existing capture pipeline runs at 16 kHz (`audio/capture.py`; rate read from
`media.get_input_audio_samplerate()`). The model self-reports its required frequency at init
(24 kHz per section 7). `prepare_wake_audio()` (section 10) therefore resamples 16 -> 24 kHz using
the model_parameters value from the hello response — never a hardcoded constant.

## A4. Reference implementation

Luis Moreau's Space (same URL as A2) is a working Reachy Mini wake-word app on this exact
hardware: model loads on aarch64, reports 24 kHz / 12000-sample slices, default threshold 0.7.
Useful as behavioral reference; its mic ownership model (own PyAudio stream) is exactly what
this spec forbids — do not copy that part.

## A5. Live protocol probe results (2026-08-21, mac-arm64 build, dev box)

The vendored-client design was proven against the real model before planning. Verified facts,
correcting section 7 where they differ:

* Hello response `model_parameters`: `input_features_count: 48000`, `slice_size: 12000`,
  `frequency: 24000`, `labels: ["hey_reachy", "noise", "other"]`,
  `model_type: "classification"`, `use_continuous_mode: true`, `thresholds: []`.
* The TRUE model window is 48,000 samples = 2.0 s. Section 7's "~12,000 samples (~500 ms)"
  describes the slice, not the window. `{"classify": [12000 samples]}` is rejected with
  "expected 48000 but got 12000".
* Detector contract therefore: maintain a rolling 48,000-sample window, send the full window as
  plain int16 values via `{"classify": [...]}` every `slice_size` new samples (0.5 s stride) —
  the official SDK's own AudioImpulseRunner does exactly this (rolling buffer, 0.25 overlap
  drop, plain classify; it never uses continuous-mode messages).
* `classify` of 48,000 zeros returned `{"hey_reachy": 0.0, "noise": 1.0, "other": 0.0}` in
  ~5 ms on an M-series Mac (response includes a `timing` block: dsp/classification ms — feed
  those into wake metrics). Expect tens of ms on the robot's ARM core; still far under budget.
* Messages must be written with `sendall` semantics (a bare socket `send` of the ~100 KB
  classify payload truncates and hangs the exchange); responses are read until the trailing
  `\x00` byte.
* The runner offers a shared-memory fast path in hello (`features_shm`); ignoring it and using
  plain JSON works — confirmed live.
* Clean shutdown: SIGINT to the runner process, close the socket, remove the temp socket dir.
