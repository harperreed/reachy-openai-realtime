<!-- Provenance: authored by Harper Reed, delivered 2026-08-17. This file is the
     source spec for the production-hardening work. Implementation plans in
     docs/superpowers/plans/ argue from this document. Do not edit casually;
     annotate corrections in the plans instead. -->

# Reachy Mini OpenAI Realtime — Production Hardening & Feature Expansion Spec

## 1. Objective

Extend `tinjyuu/reachy-openai-realtime` into a robust, appliance-like conversational runtime for **Reachy Mini Wireless** using OpenAI Realtime directly.

The target system should:

* remain a direct **Reachy ↔ OpenAI Realtime** architecture
* prioritize low conversational latency
* survive transient network, API, audio, camera, and hardware failures
* support Reachy Mini's Hugging Face emotion and dance libraries
* expose useful physical capabilities as Realtime function tools
* provide editable model, voice, reasoning, and system-prompt configuration
* provide structured logs and useful runtime diagnostics
* recover automatically from common failure modes
* remain understandable enough to maintain without introducing a large agent framework

Do **not** introduce Hermes, OpenClaw, LangChain, or a second conversational LLM into the critical voice path.

Target architecture:

```text
                  Reachy Mini Wireless
                         │
          ┌──────────────┼──────────────┐
          │              │              │
     AudioWorker    MotionManager   CameraWorker
          │              │              │
          └──────────────┼──────────────┘
                         │
                    Session FSM
                         │
              ┌──────────┴──────────┐
              │                     │
         Tool Executor        Event Recorder
              │
              ▼
        OpenAI Realtime
      persistent WebSocket
```

The existing project is a good starting point and should be evolved rather than rewritten.

---

# 2. Core design principles

## 2.1 Keep the latency-critical path minimal

Normal conversation should remain:

```text
Reachy microphone
      ↓
local turn detection
      ↓
OpenAI Realtime WebSocket
      ↓
streaming speech
      ↓
Reachy speaker
```

Do not insert separate STT, LLM, TTS, memory, or orchestration services into normal turns.

Robot function calls may execute alongside the voice interaction, but they must not block receipt or playback of Realtime audio.

## 2.2 Favor freshness over perfect audio completeness

For a conversational robot, stale audio is worse than dropped audio.

The playback system should prioritize maintaining approximately 100–300 ms of queued audio and should never accumulate multiple seconds of speech latency.

## 2.3 Every subsystem must be independently recoverable

Failures in:

* microphone
* speaker
* camera
* Realtime WebSocket
* motion control
* individual tools

must not require restarting the entire robot where a narrower restart is possible.

## 2.4 Never allow stale async work to affect a new session

Every Realtime connection must have a monotonically increasing **connection epoch**.

Any event, tool result, audio chunk, camera capture, or callback belonging to an older epoch must be discarded.

---

# 3. Explicit session state machine

Replace the current collection of loosely coupled booleans with an explicit finite state machine.

Use an enum similar to:

```python
class SessionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    INITIALIZING = auto()
    LISTENING = auto()
    USER_SPEAKING = auto()
    WAITING_RESPONSE = auto()
    ASSISTANT_SPEAKING = auto()
    INTERRUPTING = auto()
    TOOL_EXECUTION = auto()
    RECOVERING = auto()
    STOPPING = auto()
```

All state transitions should go through one method:

```python
transition(
    SessionState.ASSISTANT_SPEAKING,
    reason="first_audio_played",
)
```

Record every transition in structured logs.

Illegal transitions should generate warnings and optionally assertions in development/test mode.

Examples:

```text
DISCONNECTED
    → CONNECTING

CONNECTING
    → INITIALIZING
    → RECOVERING

INITIALIZING
    → LISTENING

LISTENING
    → USER_SPEAKING

USER_SPEAKING
    → WAITING_RESPONSE

WAITING_RESPONSE
    → ASSISTANT_SPEAKING
    → TOOL_EXECUTION
    → RECOVERING

ASSISTANT_SPEAKING
    → INTERRUPTING
    → LISTENING

INTERRUPTING
    → USER_SPEAKING

ANY
    → RECOVERING
```

---

# 4. Connection epochs and reset behavior

Maintain:

```python
self.connection_epoch: int
```

Increment it whenever a new OpenAI Realtime connection is created.

Associate the epoch with:

```python
@dataclass
class AudioChunk:
    epoch: int
    response_id: str
    pcm: np.ndarray

@dataclass
class ToolInvocation:
    epoch: int
    call_id: str
    name: str
    arguments: dict

@dataclass
class CameraRequest:
    epoch: int
```

Before executing asynchronous completion logic:

```python
if work.epoch != self.connection_epoch:
    return
```

When a connection fails, call one canonical reset method:

```python
async def reset_connection_state(self):
    ...
```

It should:

```text
cancel active response
cancel camera work
clear playback buffer
stop active motion
reset local VAD turn state
clear pending tool calls
clear pending tool outputs
clear current response ID
clear current audio item ID
reset speaker busy time
mark input disabled
mark response inactive
remove connection-specific timers
```

A reconnect must never inherit a partially active response from the prior connection.

---

# 5. Realtime watchdog and recovery

Add expectation-driven watchdogs instead of relying only on socket exceptions.

Track deadlines for protocol operations.

Suggested defaults:

```text
session.update → session.updated             5 sec
response.create → response.created           5 sec
response.created → first output event       15 sec
response.cancel → terminal event             3 sec
tool result → new response.created           5 sec
input append operation                       5 sec
camera conversation item operation           5 sec
```

If a deadline expires:

1. log the timeout
2. move state to `RECOVERING`
3. cancel the connection
4. reset connection state
5. create a fresh Realtime connection

Reconnect strategy:

```text
1 sec
2 sec
4 sec
8 sec
15 sec
30 sec maximum
```

Add ±20% jitter.

Reset the backoff after the connection has remained healthy for a configurable period, e.g. 60 seconds.

Differentiate errors where possible:

```text
transient:
    network disconnect
    server 5xx
    temporary timeout
    rate limiting

configuration/fatal:
    invalid API key
    invalid model ID
    malformed session configuration

recoverable protocol:
    individual conversation item error
    camera upload error
    tool output error
```

Fatal configuration errors should stop reconnect spam and display a clear configuration error in the UI.

---

# 6. Dedicated audio workers

Do not allow a blocking Reachy media call to indefinitely occupy an asyncio worker.

Create dedicated long-lived threads or workers.

## Audio capture

```text
Reachy media API
      ↓
capture worker
      ↓
bounded PCM queue
      ↓
async Realtime session
```

Track:

```python
last_mic_frame_at
mic_frames_total
mic_restart_count
```

Recommended bounded queue: enough for approximately 500 ms maximum.

If the async consumer falls behind, drop oldest frames.

### Audio watchdog

If no microphone frame arrives for approximately 1.5–2 seconds while recording should be active:

```text
attempt 1:
    stop_recording()
    start_recording()

attempt 2:
    restart Reachy media pipeline

attempt 3:
    restart app session
```

Do not reboot Reachy automatically.

## Speaker output

Use a dedicated output worker with a latency-based jitter buffer.

Track:

```python
last_speaker_write_at
speaker_frames_total
queued_audio_ms
speaker_restart_count
```

---

# 7. Latency-bounded playback buffer

Replace queue capacity expressed purely as number of chunks with explicit time limits.

Recommended:

```python
TARGET_BUFFER_MS = 200
MAX_BUFFER_MS = 500
HARD_MAX_BUFFER_MS = 1000
```

Behavior:

### Under target

Play normally.

### Over maximum

Drop the **oldest** queued audio until under the limit.

### Over hard maximum

Treat this as a playback failure:

```text
clear queue
cancel current response
re-enter listening state
record playback_overrun
```

Never allow Reachy to continue speaking speech that is several seconds behind the current conversation.

Expose `queued_audio_ms` in diagnostics.

---

# 8. Barge-in and interruption

Preserve the existing Realtime interruption behavior:

```text
human speech detected
       ↓
response.cancel
       ↓
clear speaker buffer
       ↓
conversation.item.truncate
       ↓
resume input
```

Improve it with:

* connection-epoch checks
* explicit FSM transition to `INTERRUPTING`
* timeout on cancellation
* latency metrics

Track:

```text
barge_in_detected_at
speaker_silent_at
```

Expose:

```text
barge_in_to_silence_ms
```

Target: as consistently low as practical.

---

# 9. Voice activity detection

Retain the current adaptive local VAD as the primary mechanism.

Use the ReSpeaker speech/DoA indication where available.

Add a fallback hierarchy:

```text
ReSpeaker speech detection
          ↓
local neural VAD
          ↓
adaptive energy VAD
```

The fallback neural VAD should be lightweight enough for the Reachy hardware. Make it optional if dependency size or CPU overhead is problematic.

During Reachy playback, barge-in must remain conservative enough to avoid self-interruption from speaker leakage.

Expose the following diagnostics:

```text
noise_floor_dbfs
start_threshold_dbfs
continue_threshold_dbfs
respeaker_speech_detected
doa_angle
vad_backend
```

Consider OpenAI semantic VAD as a secondary server-side hint, but do not make it the sole turn detector because local speaker echo is robot-specific.

---

# 10. Hugging Face emotion library

Add first-class support for:

```text
pollen-robotics/reachy-mini-emotions-library
```

Use Reachy Mini's native recorded-move support.

Create:

```python
class RecordedMoveCatalog:
    ...
```

Responsibilities:

* initialize the Hugging Face recorded move library
* enumerate available move names dynamically
* validate requested moves
* expose searchable metadata
* cache loaded move metadata
* execute through `MotionManager`
* recover gracefully if Hugging Face data is unavailable
* never require a hardcoded complete move list

Do not encode the full catalog as separate OpenAI function definitions.

Expose one tool:

```json
{
  "name": "play_emotion",
  "description": "Play a recorded Reachy Mini emotional gesture.",
  "parameters": {
    "type": "object",
    "properties": {
      "emotion": {
        "type": "string"
      }
    },
    "required": ["emotion"],
    "additionalProperties": false
  }
}
```

Validate `emotion` against the actual catalog.

Also expose:

```text
stop_emotion
```

The UI should allow:

```text
search/filter
select
preview/play
stop
```

The model should receive either:

* the available catalog names in system/session context, or
* a concise subset plus a tool to query available emotions

Avoid sending hundreds of unnecessary tokens every response.

---

# 11. Hugging Face dance library

Implement the same abstraction for:

```text
pollen-robotics/reachy-mini-dances-library
```

Tools:

```text
play_dance
stop_dance
```

Motion must execute through the same `MotionManager` arbitration system as all other movement.

---

# 12. Central MotionManager

All physical movement must go through one arbiter.

Sources include:

```text
idle breathing
listening motion
speaking motion
look direction
look-at-speaker
nod
shake head
HF emotions
HF dances
explicit stop
```

Suggested priority values:

```text
emergency/stop             100
barge-in cancellation       90
explicit tool gesture       75
HF emotion                  70
HF dance                    65
look-at-speaker             50
look                       45
speaking motion             20
listening motion            15
idle breathing              10
```

Higher-priority movement may preempt lower-priority movement.

Recorded moves should temporarily suppress background speaking/idle motions.

After an explicit emotion/dance finishes, transition smoothly back to the appropriate current background mode.

The Realtime model must never receive direct unrestricted joint-angle control.

---

# 13. Initial Realtime tool surface

Start with a small, well-defined set:

```text
look
look_at_speaker
nod
shake_head

play_emotion
stop_emotion

play_dance
stop_dance

see

stop_motion
```

## `look`

Use bounded named directions:

```text
front
left
right
up
down
```

Optionally add a safe numeric variant later.

## `look_at_speaker`

No required arguments.

Use the latest valid ReSpeaker DoA estimate and move Reachy toward it within safe physical limits.

If DoA is unavailable or stale, return:

```json
{
  "ok": false,
  "error": "speaker_direction_unavailable"
}
```

## `see`

Capture one current camera frame and insert it as an image conversation item into the active Realtime conversation.

Do not continuously send camera frames by default.

Return a short structured result indicating success or failure.

## `stop_motion`

Immediately cancel current and queued explicit motions.

---

# 14. Tool executor

Tools must not execute in the Realtime event-consumer coroutine.

Implement a dedicated asynchronous tool executor.

Each invocation should include:

```python
ToolInvocation(
    epoch,
    call_id,
    name,
    arguments,
)
```

Requirements:

* maximum bounded concurrency
* per-tool timeout
* duplicate `call_id` protection
* idempotency where relevant
* stale epoch rejection
* cancellation support
* result-size limit
* structured logging

Recommended defaults:

```text
motion tool timeout:        10 sec
camera tool timeout:         5 sec
other future tool timeout:  15 sec
max parallel tools:          2
```

Return tool errors to the model as structured JSON rather than raising into the Realtime receive loop.

Example:

```json
{
  "ok": false,
  "error": "motion_timeout"
}
```

---

# 15. Model selection

Add configurable Realtime model selection.

UI:

```text
Model
[ gpt-realtime-2.1 ▼ ]

Reasoning effort
[ low ▼ ]

Voice
[ marin ▼ ]
```

Maintain a known-model preset list, but also expose:

```text
Custom model ID
```

so future compatible models can be tested without updating the package.

Configuration object:

```python
@dataclass(frozen=True)
class RealtimeSettings:
    model: str
    voice: str
    reasoning_effort: str
```

Changing models requires reconnecting the Realtime WebSocket.

Model changes should:

1. save configuration
2. end current response safely
3. reset session state
4. reconnect using new model
5. preserve recent textual conversation context where possible

Expose model name in every relevant structured log entry.

---

# 16. System prompt editor

Add an editable user/personality prompt.

Do not allow the user-editable prompt to replace the entire robot-control contract.

Compose final instructions from separate layers:

```python
final_prompt = "\n\n".join([
    RUNTIME_INSTRUCTIONS,
    user_system_prompt,
    language_instructions,
    optional_runtime_context,
])
```

## Runtime instructions

Hardcoded/version-controlled instructions should cover:

* Reachy identity
* physical safety rules
* use only exposed tools
* never invent recorded-move names
* concise spoken responses
* motion tools should support, not distract from, conversation
* visual questions may use `see`
* no direct joint-angle requests
* do not repeatedly perform movements without conversational reason

## User-editable prompt

Examples:

```text
You are Reachy, an enthusiastic but concise robot assistant.
You like dry humor.
Don't over-explain.
```

UI:

```text
SYSTEM PROMPT

┌────────────────────────────────────────────┐
│ ...editable text...                        │
└────────────────────────────────────────────┘

[ Reset to default ] [ Save ] [ Apply ]
```

Persist:

```text
prompt
prompt revision number
prompt SHA-256
updated timestamp
```

Log only revision/hash by default, not necessarily the full prompt on every event.

Allow export through diagnostics if explicitly requested.

---

# 17. Configuration persistence

Store persistent configuration beneath the existing Reachy app config location.

Suggested:

```text
~/.config/reachy-mini/apps/reachy_openai_realtime/
    .env
    settings.json
    usage.json
    events.jsonl
    application.log
```

Example `settings.json`:

```json
{
  "model": "gpt-realtime-2.1",
  "voice": "marin",
  "reasoning_effort": "low",
  "language": "en",
  "system_prompt": "You are Reachy...",
  "enabled_tools": [
    "look",
    "look_at_speaker",
    "nod",
    "shake_head",
    "play_emotion",
    "stop_emotion",
    "play_dance",
    "stop_dance",
    "see",
    "stop_motion"
  ],
  "camera_enabled": false
}
```

Use atomic file replacement when saving JSON.

Never expose the OpenAI API key through a diagnostics API.

---

# 18. Structured logging / flight recorder

Keep normal human-readable logs:

```text
application.log
```

Add structured JSONL:

```text
events.jsonl
```

Example:

```json
{
  "timestamp": "2026-08-17T19:41:24.381-05:00",
  "event": "response.first_audio",
  "connection_epoch": 14,
  "response_id": "resp_123",
  "model": "gpt-realtime-2.1",
  "latency_ms": 683
}
```

Every event should have:

```text
timestamp
event
connection_epoch
session_state
```

where applicable.

Record at least:

```text
app.start
app.stop

fsm.transition

audio.capture.started
audio.capture.stopped
audio.capture.stalled
audio.capture.restarted

audio.playback.started
audio.playback.overrun
audio.playback.restarted

vad.started
vad.stopped

realtime.connecting
realtime.connected
realtime.disconnected
realtime.error
realtime.reconnect

response.requested
response.created
response.first_audio_received
response.first_audio_played
response.completed
response.cancelled
response.interrupted

tool.requested
tool.started
tool.completed
tool.failed
tool.timeout

motion.started
motion.completed
motion.cancelled

emotion.started
emotion.completed

dance.started
dance.completed

camera.capture.started
camera.capture.completed
camera.capture.failed

settings.changed
model.changed
voice.changed
prompt.changed

watchdog.triggered
```

Never store:

* API keys
* authorization headers
* raw microphone audio by default

Transcripts should be optional/configurable.

---

# 19. Latency and reliability metrics

Track:

```text
speech_end_to_response_created_ms
speech_end_to_first_audio_received_ms
speech_end_to_first_audio_played_ms
audio_receive_to_playback_ms

barge_in_to_cancel_ms
barge_in_to_silence_ms

tool_duration_ms

queued_audio_ms
mic_frame_age_ms
doa_age_ms

connection_uptime_seconds
reconnect_count
mic_restart_count
speaker_restart_count
tool_error_count
```

Maintain aggregate:

```text
count
min
max
mean
p50
p95
```

where useful.

Expose current and recent values in `/api/diagnostics`.

---

# 20. Conversation recovery after reconnect

A temporary Wi-Fi interruption should not cause immediate conversational amnesia.

Maintain a small in-memory textual ring buffer:

```text
last 6–10 user/assistant turns
```

Enable user input transcription through the Realtime session.

Treat transcripts as approximate recovery metadata, not as a replacement for the direct audio path.

On a fresh connection:

1. initialize session
2. reinsert recent text context as conversation items
3. resume listening

Do not replay prior audio.

Optional later enhancement:

```text
rolling compact conversation summary
```

Do not add an additional LLM call to produce that summary in the first implementation.

---

# 21. Camera subsystem

Create a dedicated camera abstraction.

Capabilities:

```text
capture JPEG
preview JPEG
send one frame to Realtime
health check
```

Camera failure should never kill audio conversation.

`see` should:

```text
capture frame
validate epoch
insert image conversation item
await acknowledgment with timeout
clean up old image items as needed
return success
```

Keep AI camera use off by default for privacy and cost.

The UI can independently show local preview while AI vision remains disabled.

---

# 22. Management UI

Expand the existing Reachy Mini app settings UI into a lightweight realtime dashboard.

## Status header

Show:

```text
Connected / Reconnecting / Error
model
voice
current FSM state
connection uptime
latest speech→audio latency
```

## Model

Controls:

```text
model selector
custom model ID
voice selector
reasoning effort
apply/reconnect
```

## Prompt

Large editable textarea:

```text
reset
save
apply
```

## Tools

Checkboxes:

```text
Vision
Emotions
Dances
Look
Look at speaker
Nod / shake
```

Disable corresponding Realtime tool definitions when unchecked.

## Emotion browser

```text
search
catalog selector
play
stop
```

## Dance browser

Same pattern.

## Camera

```text
local preview
AI vision toggle
capture test
```

## Audio diagnostics

Show:

```text
input level
noise floor
VAD threshold
DoA
mic frame age
playback queued ms
```

## Logs

Streaming/recent event view:

```text
timestamp
category
message
latency
```

Buttons:

```text
Download diagnostics
Download logs
Clear logs
```

---

# 23. Health endpoints

Add:

```text
GET /api/health
GET /api/status
GET /api/diagnostics
```

`/api/health` should be simple and machine-friendly:

```json
{
  "ok": true,
  "realtime": true,
  "microphone": true,
  "speaker": true,
  "motion": true,
  "camera": true
}
```

Return `ok=false` if critical conversational components are unhealthy.

Camera should not make global health fail if camera functionality is disabled.

---

# 24. Self-healing supervisor

Implement a supervisor responsible for subsystem recovery.

Possible health checks:

```text
Realtime event recency
microphone frame recency
speaker worker heartbeat
motion thread heartbeat
camera call duration
FSM inactivity
```

Suggested behavior:

```text
microphone stalled
    → restart audio capture

speaker stalled
    → restart playback pipeline

Realtime stalled
    → reconnect WebSocket

tool worker stalled
    → cancel invocation

motion worker crashed
    → recreate MotionManager

multiple repeated subsystem failures
    → restart entire application runtime
```

Avoid automatically rebooting Reachy OS.

Record every automatic recovery action.

---

# 25. Graceful degradation

The robot should continue functioning when optional features fail.

Examples:

### Hugging Face emotion catalog unavailable

```text
disable emotion tools
continue voice conversation
show UI warning
```

### Camera unavailable

```text
disable see tool
continue voice
```

### DoA unavailable

```text
fall back to other VAD
look_at_speaker returns unavailable
```

### Motion unavailable

```text
disable physical tools
continue speaking
```

### Invalid custom model

```text
show model error
allow UI to change configuration
do not reconnect infinitely
```

---

# 26. Testing

Preserve existing tests and significantly expand them.

## Unit tests

Cover:

```text
FSM legal/illegal transitions
connection epoch rejection
playback buffer dropping policy
watchdog deadline behavior
reconnect backoff
tool timeout
tool duplicate call IDs
recorded move validation
settings migration
atomic config writes
structured-log redaction
```

## Mock Realtime integration tests

Build a fake Realtime transport that can emit scripted events.

Test:

```text
normal conversation
tool call
multiple tool calls
barge-in
camera tool
response cancellation
```

## Chaos/failure tests

Mandatory cases:

```text
disconnect while listening

disconnect during user speech

disconnect immediately after
input_audio_buffer.commit

disconnect after response.create

disconnect halfway through
assistant speech

disconnect during barge-in

duplicate response.done

response.done never arrives

response.created never arrives

tool result arrives after reconnect

camera capture hangs

camera item creation fails

DoA disappears

microphone returns no frames

get_audio_sample blocks

speaker playback blocks

playback queue exceeds maximum

motion worker crashes

recorded emotion throws

OpenAI returns rate limit

OpenAI returns temporary 5xx

OpenAI key invalid

Wi-Fi unavailable for 30 seconds
```

## Physical soak test

Run on a Reachy Mini Wireless continuously for at least one overnight test.

Measure:

```text
number of reconnects
number of manual interventions
audio pipeline recoveries
memory growth
thread/task growth
latency distribution
false barge-ins
missed barge-ins
```

Success criterion:

```text
No manual restart required for recoverable failures.
```

---

# 27. Resource-leak checks

During reconnect/soak tests verify:

```text
asyncio task count remains bounded
thread count remains bounded
camera tasks are cleaned up
audio workers do not duplicate
motion workers do not duplicate
old WebSockets are closed
playback buffers are released
interrupted response IDs do not grow forever
```

Keep interrupted-response IDs bounded or expire them.

---

# 28. Suggested module layout

Refactor toward:

```text
reachy_openai_realtime/
    main.py
    config.py
    settings.py

    session/
        fsm.py
        realtime.py
        watchdog.py
        recovery.py
        context.py

    audio/
        capture.py
        playback.py
        vad.py
        metrics.py

    motion/
        manager.py
        builtin.py
        recorded_moves.py
        emotions.py
        dances.py

    camera/
        worker.py

    tools/
        executor.py
        definitions.py
        motion_tools.py
        vision_tools.py

    observability/
        events.py
        metrics.py
        diagnostics.py

    web/
        api.py
        static/
```

Do not perform a giant refactor before behavior is covered by tests.

Incrementally move existing logic behind new abstractions.

---

# 29. Recommended implementation order

## Phase 1 — reliability foundation

Implement first:

1. explicit FSM
2. connection epochs
3. canonical connection-state reset
4. watchdog deadlines
5. reconnect with jittered exponential backoff
6. audio capture worker
7. bounded playback jitter buffer
8. audio watchdogs
9. structured event recorder

No major feature expansion until these are stable.

## Phase 2 — motion architecture

Implement:

1. central `MotionManager`
2. priority/preemption
3. migrate existing look/nod/shake/express functionality
4. add Hugging Face recorded-move abstraction
5. emotions
6. dances

## Phase 3 — tools

Implement:

```text
look
look_at_speaker
nod
shake_head
play_emotion
stop_emotion
play_dance
stop_dance
see
stop_motion
```

Route all calls through `ToolExecutor`.

## Phase 4 — configuration/UI

Implement:

```text
model selector
custom model ID
voice selector
reasoning effort
system prompt editor
tool enable/disable
emotion browser
dance browser
camera controls
diagnostics/log UI
```

## Phase 5 — recovery context

Implement:

```text
user transcription
recent textual conversation ring
context reinjection after reconnect
```

## Phase 6 — soak and chaos testing

Run repeated scripted and physical tests until common faults self-heal.

---

# 30. Acceptance criteria

The implementation is considered successful when all of the following are true.

## Conversation

* speech-to-speech still uses one direct OpenAI Realtime connection
* normal responses begin streaming without an intermediate STT/LLM/TTS pipeline
* barge-in works reliably
* stale assistant audio does not accumulate

## Recovery

* network loss automatically reconnects
* reconnect cannot accidentally play audio from an old connection
* mic pipeline stalls are automatically detected and restarted
* playback stalls recover automatically
* optional subsystem failures do not stop voice conversation
* invalid configuration produces a stable error state instead of an infinite crash loop

## Motion

* all movement passes through `MotionManager`
* Hugging Face emotions are dynamically discoverable and playable
* Hugging Face dances are dynamically discoverable and playable
* background motion does not fight recorded moves
* stop always preempts motion

## Tools

* all tools have bounded execution time
* old tool responses cannot contaminate a new connection
* duplicate calls do not execute twice
* no raw unrestricted motor control is exposed

## Configuration

* model can be changed through UI
* custom model IDs are supported
* voice can be changed
* reasoning effort can be changed
* system/personality prompt can be edited
* runtime safety/control instructions remain protected
* configuration survives application restart

## Observability

* structured JSONL events exist
* useful latency metrics are captured
* reconnect/recovery events are visible
* diagnostics expose audio and connection health
* logs contain no API keys
* raw audio is not retained by default

## Stability

* repeated reconnects do not leak tasks or threads
* playback queue stays bounded
* tool queues stay bounded
* overnight operation does not require routine manual intervention

---

# 31. Constraints for implementation agent

When implementing this spec:

* work against the existing `tinjyuu/reachy-openai-realtime` architecture rather than replacing it wholesale
* preserve current working behavior unless intentionally superseded
* add tests before or alongside major state-machine changes
* favor simple Python and asyncio/thread primitives over additional frameworks
* do not introduce Hermes, OpenClaw, LangChain, or another agent runtime
* keep OpenAI Realtime as the sole conversational model path
* keep physical motor commands bounded and validated
* make optional dependencies gracefully degradable
* never store or return the OpenAI API key
* do not commit generated secrets or local configuration
* avoid one massive refactor commit; make changes in logically reviewable stages
* after each major phase, run tests and linting
* add documentation for new configuration and recovery behavior

When an implementation choice is ambiguous, optimize in this order:

```text
1. robustness
2. conversational latency
3. safety of robot movement
4. simplicity
5. feature completeness
```

---

# 32. Final desired behavior

The end result should feel less like a demo process and more like a network appliance:

```text
power Reachy on
      ↓
app starts
      ↓
audio initializes
      ↓
Realtime connects
      ↓
Reachy is conversational
      ↓

Wi-Fi glitches?
self-heals.

Mic wedges?
self-heals.

Realtime socket dies?
self-heals.

Camera breaks?
voice still works.

Recorded emote fails?
conversation continues.

User interrupts Reachy?
Reachy stops almost immediately.

Want a new personality/model?
change it in the dashboard.

Want Reachy to react physically?
Realtime calls safe native tools.

Something weird happened overnight?
events.jsonl tells us exactly what happened.
```

That is the target.
