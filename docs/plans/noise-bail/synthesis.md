# Jam synthesis — stop Reachy from conversing with noise

Five independent proposals (`panel/proposals/*.md`). This folds them into four
buildable variants and names a recommendation. Design bias is fixed:
**bail toward sleep** — never runs away; cutting off a real person in a noisy
room occasionally is an accepted price.

## Where the panel converged (4 of 5, independently)

- **Slot 2 is basically settled.** A per-session **consecutive-noise-turn
  counter** that **bails to wake-armed sleep at N=3**, resets on any real turn,
  and **short-circuits `response.create` on the triggering turn** so the model
  never answers the audio that trips the bail. Nyx, Pixel, Juno, Sarge all land
  here; Dizzy adds the same as a backstop. Bail = set the stop event; the
  teardown fix (`3036352`) turns that into a real session end → sleep.
- **Silent bail** (Dizzy, Nyx, Pixel, Sarge). Juno alone wants one spoken line.
- **Wake word is the re-entry.** Unanimous. No reconnect on a noise bail.

## Where they diverge — Slot 1 (what signal says "this turn was noise")

1. **DoA + duration heuristic** (Juno, Sarge) — a turn is noise if the ReSpeaker
   never confirmed speech during it (`doa_speech_detected is True` never seen)
   **and** committed audio was short (< ~1500 ms). Free, uses signals already
   sampled, degrades to duration-only when DoA is absent/stale.
2. **VAD-gate fix at the signal layer** (Dizzy) — stop noise turns from *starting*:
   treat `None` as not-speech (fail safe) and require sustained DoA speech
   (~160 ms) before the gate opens. The cure, not the tourniquet.
3. **Transcript** (Pixel) — enable `input_audio_transcription`; an empty /
   non-lexical transcript is the noise signal. Best signal, doesn't drift with
   room acoustics; costs a product setting + one wasted round-trip per garbage
   turn.
4. **Human-engagement flag** (Nyx) — plus two hard ceilings (turn budget + a
   30-minute wall clock) that fire regardless of the detector.

## One thing I verified in the code (it moves the recommendation)

Dizzy's headline change — `speech_detected is not False` → `is True` on
`vad.py:69` — is **not** a safe one-character edit. `doa_speech_detected`
defaults to `None` (`realtime.py:460`) and is only updated when a poller exists
(`:538`). So `None` is overloaded:

- **No ReSpeaker** → `None` forever → today's `is not False` lets energy decide
  (correct). Flip to `is True` and the gate never opens → an energy-only robot
  goes deaf.
- **ReSpeaker present, stale read** → transient `None` → today fails *open* (the
  hole we want to close).

A correct gate fix has to disambiguate those two `None`s in the caller, then add
the sustained-DoA accumulator. That is real work with real weak-mic tension
(too strict = deaf to a quiet voice), and it wants tuning on the actual robot.
This is concrete evidence for the sequence we already agreed: **backstop now,
gate fix as a later live on-robot session.**

Note: Nyx's per-turn `_last_turn_was_human` flag can't discriminate — a noise
turn also opens from LISTENING via the energy gate, so the flag reads True on
noise too. Nyx's real value is the **detector-agnostic ceilings**, which pair
with any Slot-1 signal. The synthesis takes those.

## The four variants

### V1 — Tourniquet (backstop only)  ·  RECOMMENDED, matches agreed sequence
- **Slot 1:** DoA-confirmed + duration heuristic (Juno/Sarge). Per turn, in the
  record loop: reset `_turn_had_doa_true=False` and `_turn_audio_ms=0` on
  `decision.started`; set the flag when `doa_speech_detected is True`; accumulate
  audio ms while `speech_active`. On `decision.stopped`,
  `noise = (not _turn_had_doa_true) and (_turn_audio_ms < 1500)`.
- **Slot 2:** `_consecutive_noise_turns`; reset on any real turn (DoA-confirmed
  or ≥ 1.5 s). At N=3, short-circuit before `response.create` and set the stop
  event → teardown fix ends the session → sleep, wake-armed. Silent.
- **Plus** Nyx's wall-clock ceiling in the supervisor (default 30 min,
  env-tunable) as a dumb, independent final backstop.
- **Touches:** `realtime.py` (record loop), `config.py` (env knobs),
  `runtime_status.py` (bail events), `session/supervisor.py` (wall clock).
  **Not** `vad.py`.
- **Cost/risk:** smallest change that bounds the worst case (~60 LOC + tests).
  Detection is deliberately dumb; the ceilings do the work. Cuts off a real
  quiet/short speaker after 3 short turns (accepted). Zero VAD risk.

### V2 — Tourniquet + gate fix (pull the cure forward)
- V1, **plus** Dizzy's front-end done correctly: disambiguate the two `None`s in
  `realtime.py`, treat stale-read `None` as not-speech, add the sustained-DoA
  accumulator (~160 ms) in `vad.py`. Fewer noise turns even start.
- **Cost/risk:** more code, touches `vad.py` + caller, real weak-mic tension,
  best validated live. Collapses our two agreed phases into one and does the
  riskier half blind. Only if we want it all now.

### V3 — Transcript-truth backstop
- **Slot 1:** enable `input_audio_transcription`; classify empty / non-lexical
  transcript as noise (Pixel's tiers). **Slot 2:** same counter → silent bail, N=3.
- **Cost/risk:** turns transcription on (a product setting); one wasted model
  round-trip per garbage turn (cancel-after-create); more event-loop moving
  parts. Best signal quality, no DoA weak-mic tension. Marginal token cost
  (audio already billed).

### V4 — Graceful nap (V1 + designed bail UX)
- V1 detection **plus** Juno's staged response: silent on turns 1–2 (skip
  `response.create`), one quiet spoken line on turn 3 — "I'll rest. Say 'hey
  reachy' when you need me." — head bow, then sleep; warm wake.
- **Cost/risk:** one scoped `response.create` on the bail turn (short override;
  small risk the model over-talks). Everything else = V1. This is the only
  "does it speak on the way out" decision, and it's a cheap, reversible delta.

## Recommendation

Build **V1 now**. It is the tourniquet we already agreed to build, four of five
personas converged on its exact shape, it's the smallest change, and it carries
zero risk to voice pickup because it never touches the VAD. Fold in Nyx's
wall-clock ceiling — cheap, independent, strictly additive.

Hold **V2's gate fix** for the later live on-robot session, exactly as planned —
now with concrete evidence (the two-`None` problem) for why it must be live, not
blind.

Offer **V4's one-line farewell** as an optional add-on to V1 if we want the bail
to feel deliberate rather than abrupt. It's the single UX knob and it's low-risk.

Keep **V3** in reserve: the strongest alternative signal, worth reaching for only
if the DoA+duration heuristic proves too crude in practice.

## Addendum — we are building V3 + the ceiling, not V1 (decided 2026-08-23)

Reading the commit path before coding V1 showed its DoA+duration classifier is
mechanically defeated on this robot, so V1's fast-bail signal is worthless:

- **"DoA never `True` during the turn" fails.** The ReSpeaker fires
  `speech_detected=True` on ~27% of ambient samples (live: 4/15). A committed
  turn spans ~14 samples, so P(at least one `True`) ≈ 99%. Almost every noise
  turn trips `_turn_had_doa_true` → read as human.
- **"committed audio < 1500 ms" barely discriminates.** The VAD flushes a
  pre-roll (≤350 ms) then appends every frame through the full 800 ms
  trailing-silence countdown, so the floor for *any* committed turn is
  ~1390 ms. The 1500 ms line sits right on top of every turn's minimum.

The wall-clock ceiling (Nyx) never depended on the classifier, so it survives.
The fix is to swap V1's broken signal for **V3's transcript signal**, which does
not drift with room acoustics:

- **Guarantee (free):** session wall clock, measured from session start so it
  survives reconnects. Over the limit → set the stop flag → sleep. No
  classification, so nothing can defeat it.
- **Fast bail:** enable input transcription; a committed turn whose transcript
  has no word characters is noise. N consecutive → set the stop flag → sleep.
  Resets on any real transcript and on reconnect.

Corrections to the panel's cost and classifier claims, verified in the SDK
(openai 2.53.0) and the turn flow:

- Pixel's "near zero cost — audio already billed" is **wrong**. The completed
  event carries `usage` token/duration accounting; input transcription is a
  separate metered pass that runs on *every* committed turn, not just garbage
  ones. Small per turn, but real and always-on. It is an accepted cost of the
  chosen option, not free.
- Dropped Pixel's tier 3 ("≤3 chars, no Latin vowel"): it misclassifies short
  CJK words ("好") as noise. Tiers 1–2 (empty / no `\w`) are Unicode-safe —
  `\w` matches CJK — and the ceiling backstops whatever the classifier misses
  (including Whisper hallucinating "you"/"thank you" on noise).

This is the "Ceiling + transcript" build. V2's gate fix stays deferred to the
live on-robot session, unchanged.
