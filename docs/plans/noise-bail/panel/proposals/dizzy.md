# Dizzy's Proposal — Stop Reachy from Conversing with Noise

Worldview: Fix it where the signal lives. A gate that never opens on noise makes
backstops moot. The right answer is a front-end that rejects ambient blips before
any audio reaches OpenAI — not a retry counter bolted on after the damage is done.

---

## Slot 1 — Detection

### The two holes to close (both in `vad.py` `process`, pre-turn gate)

**Hole A: `is not False` opens on `None`.**
`None` means "no fresh DoA reading" — the ReSpeaker is silent, disconnected, or
stale. Treating it as "not False" is a fail-open default. It should be fail-safe:
treat `None` as `False` (no confirmed speech).

Change in `vad.py` `process()`, pre-turn branch (line 69):
```python
# Before
voice_gate_open = speech_detected is not False
# After
voice_gate_open = speech_detected is True
```
This single character-level change makes both the respeaker+energy and
energy-only backends behave identically when DoA data is missing: gate closed.
It also makes the between-turns gate as strict as the during-playback gate —
removing the asymmetry that was the original root cause.

**Hole B: The ReSpeaker's `speech_detected` fires on ambient sound.**
The live data showed ~4/15 samples fire `True` on nobody — random blips, not
sustained speech. The fix: require *sustained* `speech_detected` before the energy
gate can open. Add a `_doa_speech_ms` accumulator to `EnergyTurnDetector` (new
field, zero-initialized, reset alongside `_candidate_ms`):

- While `speech_detected is True` and energy is above start threshold:
  accumulate BOTH `_candidate_ms` (already there) AND `_doa_speech_ms`.
- While `speech_detected is not True` (False or None):
  **reset both** `_candidate_ms` and `_doa_speech_ms` to zero.
- Turn starts only when `_candidate_ms >= min_speech_ms` AND
  `_doa_speech_ms >= doa_min_speech_ms` (new param, default: 160 ms).

The threshold of 160 ms is half of `min_speech_ms` (240 ms). A real human
utterance holds `speech_detected=True` continuously. Ambient blips last one or
two 100 ms DoA poll intervals, so ≤200 ms sustained DoA is a reliable
ambient-blip filter without clipping the leading edge of real speech.

When `_doa_poller is None` (no ReSpeaker), `speech_detected` is always `None` —
which with Hole A closed means the gate never opens via this path. That's correct:
fall back to pure energy VAD exactly as before, but with the DoA channel
fail-safe.

**No angle-stability filter.** The angle jitter is a symptom of the blip, not
independently useful. The sustained-speech requirement is sufficient and
mechanistically simpler. Don't add angle hysteresis unless sustained-DoA fails
in practice.

### Changes (minimal, surgical)

- `vad.py`: `EnergyTurnDetector.__init__` — add `doa_min_speech_ms: float = 160.0`;
  add `self._doa_speech_ms = 0.0`.
- `vad.py`: `process()` pre-turn branch — change `is not False` → `is True` for
  `voice_gate_open`; accumulate / reset `_doa_speech_ms`; add DoA persistence
  guard to the `started` condition.
- `vad.py`: `reset_turn()` — reset `self._doa_speech_ms = 0.0`.
- `realtime.py`: no change needed — the `speech_detected` kwarg already threads
  through correctly; the `energy-only` fallback path stays intact.
- `config.py`: expose `doa_min_speech_ms` on `AppConfig` with an env override
  (`REACHY_OPENAI_REALTIME_DOA_MIN_SPEECH_MS`) so it can be tuned without a
  deploy.

Estimate: ~30 lines changed/added across two files.

---

## Slot 2 — Response

**Minimal. One backstop. Bail toward sleep.**

Add a consecutive-noise-turn counter (`_noise_turn_streak: int`) to
`RealtimeRobotSession`, reset to zero whenever a turn commits and the Realtime
API returns a response with any non-trivial content (proxy: response length > 0
characters in the `response.done` event). Increment it when a turn commits and
the model's response is empty or the turn duration is below a short-utterance
floor (< 600 ms of audio committed — a noise blip that the model also found
empty).

When `_noise_turn_streak >= 3`: set the stop event. The caller's run-loop exits,
which with the teardown fix (3036352) actually ends the session. Main.py's
supervisor will treat this as a clean stop and re-enter the wake-word loop.
No spoken response, no motion, no reconnect attempt — just sleep.

Three strikes is enough. The design bias says aggressive early sleep over staying
engaged. If a real person is being cut off after three unrecognized turns in a row,
they'll say "hey reachy" and re-engage — which costs them 2 seconds and costs us
nothing.

**No "I only hear noise" reply.** If Slot 1 is working, the gate closes before
audio commits and the response path is never reached. Slot 2 is for the rare
case where energy VAD opens a turn but the model finds nothing — it catches what
Slot 1 misses. The spoken "I only hear noise" reply is the runaway mechanism
itself: it keeps the FSM active, prevents idle, and fills the 60-minute cap.
Remove it if it exists; replace it with silence.

Implementation: ~20 lines in `realtime.py` around `decision.stopped` and the
`response.done` event handler.

---

## Why

The ReSpeaker's `speech_detected` bit is a noisy signal, not a reliable gate.
But it has one property that ambient blips don't: real human speech holds it
`True` for hundreds of milliseconds continuously. The sustained-DoA requirement
exploits that property directly. It doesn't require transcription, doesn't add
API calls, and costs zero latency on real speech because real speech accumulates
the 160 ms persistence before `min_speech_ms` (240 ms) fires anyway.

Treating `None` as `False` is a correctness fix with no trade-off. The existing
comment in the code already says "the ReSpeaker's explicit human-speech signal may
open the gate" during playback — between-turns should have the same semantics.
It currently doesn't, and that asymmetry is the root cause.

Slot 2 exists because I don't trust that Slot 1 catches every edge — especially
on marginal hardware or firmware updates that change the DoA polling rate. A
streak counter is three lines of logic with no false-positive risk in normal use.

---

## Cost, risks, trade-offs

**Risk: too-strict gate in real use.** A quiet speaker in a noisy room might not
sustain `speech_detected=True` for 160 ms before `min_speech_ms` fires. This is
a real weak-mic tension. Mitigations:
- 160 ms is well below 240 ms, so if the ReSpeaker is tracking a real voice at
  all, the DOA persistence is satisfied before the energy gate opens.
- If the ReSpeaker firmware is not present / not polling, the behavior falls back
  to energy-only VAD (same as before this change), which is acceptable.
- The env-var override lets the threshold be tuned down to 0 in the field without
  a deploy.

**Risk: Slot 2 strikes on a real but quiet conversation.** A person whispering
three short questions in a noisy room could trigger the streak. Acceptable per
the product owner's stated bias: bail toward sleep. Re-engage with wake word.

**Interaction with the teardown fix (3036352):** Slot 2 depends on it. Setting
the stop event without the teardown fix would not actually end the session. The
proposal explicitly assumes 3036352 is merged.

**No cost on the happy path.** When the ReSpeaker correctly holds `True` during
real speech, the sustained-DoA check passes on the same frame that `_candidate_ms`
satisfies `min_speech_ms`. Zero added latency.

---

## How I'd test it (TDD — failing tests first)

### Test 1 — `None` treated as `False` (Hole A)

```python
def test_none_doa_does_not_open_gate():
    vad = EnergyTurnDetector()
    # Feed enough energy that bare energy would open the gate,
    # but speech_detected=None throughout.
    for _ in range(20):
        result = vad.process(-40.0, 20.0, speech_detected=None)
    assert not vad.speech_active
    assert all not d.started for d in ...)  # no started decision ever emitted
```

Fails before the fix (`is not False` opens on `None`). Passes after.

### Test 2 — single blip does not open gate

```python
def test_single_doa_blip_does_not_open_gate():
    vad = EnergyTurnDetector(doa_min_speech_ms=160.0)
    # One 100 ms frame with speech_detected=True at high energy, then None.
    vad.process(-40.0, 100.0, speech_detected=True)
    result = vad.process(-40.0, 100.0, speech_detected=None)
    assert not vad.speech_active
```

Fails before sustained-DoA requirement. Passes after.

### Test 3 — sustained DoA opens gate normally

```python
def test_sustained_doa_opens_gate():
    vad = EnergyTurnDetector(doa_min_speech_ms=160.0, min_speech_ms=240.0)
    decisions = [vad.process(-40.0, 20.0, speech_detected=True) for _ in range(15)]
    assert any(d.started for d in decisions)
```

Passes after; verifies no regression on real speech.

### Test 4 — noise-turn streak triggers sleep (Slot 2)

Integration-level test using a fake stop event and a stubbed Realtime connection.
Feed three turns where `response.done` carries zero output tokens. Assert the stop
event is set after the third. Does not test the stopped session teardown (that
belongs to the 3036352 suite).

### Test 5 — streak resets on real content

Same setup; two noise turns, then one turn with non-empty model output. Assert
streak resets to zero and stop event is not set.
