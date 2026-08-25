# ABOUTME: PresenceManager owns the sleep/wake lifecycle — it runs the wake-word
# ABOUTME: worker while asleep and builds/tears down a realtime session on wake.
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..session.recovery import SessionOutcome
from ..wakeword.buffer import AudioRingBuffer
from ..wakeword.worker import WakeEvent, WakeWordWorker
from .states import PresenceState, PresenceStateMachine

if TYPE_CHECKING:
    from ..audio.capture import CaptureWorker
    from ..audio.fanout import AudioFrame, AudioSubscription
    from ..motion import MotionManager
    from ..runtime_status import RuntimeStatus
    from ..wakeword.base import WakeWordDetector

logger = logging.getLogger(__name__)

# Bound the initial connect (spec §19/§20): if a woken session never reaches
# AWAKE within this window, abort back to SLEEPING instead of retrying forever.
_CONNECT_TIMEOUT_SECONDS = 10.0

# The wake subscription only has to bridge the gap between the worker's pops;
# drop-oldest past this keeps stale audio out of the classifier (spec §38).
_WAKE_SUBSCRIPTION_BUFFER_MS = 2_000.0


class _EitherStop:
    """Combined stop signal: set when either the app is shutting down or this
    session has been told to end (manual sleep, failed connect). The realtime
    session reads ``is_set()`` only; ``wait`` is a polling fallback so this is a
    complete stand-in for a ``threading.Event``."""

    def __init__(self, primary: Any, secondary: threading.Event) -> None:
        self._primary = primary
        self._secondary = secondary

    def is_set(self) -> bool:
        return self._primary.is_set() or self._secondary.is_set()

    def set(self) -> None:
        """Request this session to stop without stopping the whole app."""
        self._secondary.set()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True


class _WakeStartupStop:
    """Wake-session stop signal that makes the startup deadline authoritative."""

    def __init__(
        self,
        primary: Any,
        secondary: threading.Event,
        *,
        lifecycle_lock: Any,
        startup_resolved: threading.Event,
        deadline_expired: threading.Event,
        deadline_at: float,
        clock: Callable[[], float],
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._lifecycle_lock = lifecycle_lock
        self._startup_resolved = startup_resolved
        self._deadline_expired = deadline_expired
        self._deadline_at = deadline_at
        self._clock = clock

    def _resolve_deadline_locked(self) -> bool:
        if self._startup_resolved.is_set() or self._clock() < self._deadline_at:
            return False
        self._deadline_expired.set()
        self._startup_resolved.set()
        return True

    def resolve_deadline(self) -> bool:
        if self._clock() < self._deadline_at:
            return False
        with self._lifecycle_lock:
            deadline_won = self._resolve_deadline_locked()
        if deadline_won:
            self._secondary.set()
        return deadline_won

    def is_set(self) -> bool:
        self.resolve_deadline()
        return self._primary.is_set() or self._secondary.is_set()

    def set(self) -> None:
        self._secondary.set()

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True


class WakeAudioAssembler:
    """Collects the wake pre-roll: the short slice of history just before the
    detection timestamp (spec §17/§18). Returns RAW ``AudioFrame``s — the
    session owns the single encode path (spec §10). Post-wake and connect-window
    audio arrive through the session's own live subscription, not from here."""

    def __init__(self, ring_buffer: AudioRingBuffer, *, pre_roll_seconds: float = 0.4) -> None:
        self._ring_buffer = ring_buffer
        self._pre_roll_seconds = pre_roll_seconds

    def collect(self, detected_at: float) -> list[AudioFrame]:
        return self._ring_buffer.since(detected_at - self._pre_roll_seconds)


@dataclass
class _PendingWake:
    """One armed wake, waiting for the run loop to build its session."""

    wake_audio: list[AudioFrame] | None
    event: WakeEvent | None
    stop_event: threading.Event
    cancel_reason: str | None = None


class PresenceManager:
    """Owns BOOTING→SLEEPING→WAKING→AWAKE and the session lifecycle.

    While asleep, a :class:`WakeWordWorker` classifies mic audio and fills a
    pre-roll ring buffer. On wake (word or manual) the manager acknowledges with
    motion, builds a ready-gated realtime session without replaying the captured
    pre-roll, and flips to AWAKE once the input gate opens. A failed connect
    returns to SLEEPING (spec §20); a missing wake model boots to ERROR but still
    honours manual wake (spec §21).
    """

    def __init__(
        self,
        *,
        capture: CaptureWorker,
        detector: WakeWordDetector | None,
        motion: MotionManager,
        session_factory: Callable[..., Any],
        status: RuntimeStatus,
        history_seconds: float = 4.0,
        pre_roll_seconds: float = 0.4,
        debounce_seconds: float = 2.0,
        stall_seconds: float = 2.0,
        boot_motion_enabled: bool = True,
        wake_motion_enabled: bool = True,
        connect_timeout_seconds: float = _CONNECT_TIMEOUT_SECONDS,
        on_transition: Callable[[PresenceState, PresenceState, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capture = capture
        self._detector = detector
        self._motion = motion
        self._session_factory = session_factory
        self._status = status
        self._debounce_seconds = debounce_seconds
        self._stall_seconds = stall_seconds
        self._boot_motion_enabled = boot_motion_enabled
        self._wake_motion_enabled = wake_motion_enabled
        self._connect_timeout_seconds = connect_timeout_seconds
        self._external_on_transition = on_transition
        self._clock = clock

        self._states = PresenceStateMachine(on_transition=self._handle_transition)
        self._ring_buffer = AudioRingBuffer(history_seconds=history_seconds)
        self._assembler = WakeAudioAssembler(self._ring_buffer, pre_roll_seconds=pre_roll_seconds)

        # _lock guards lifecycle state; it is always taken BEFORE the state-machine
        # lock (never the reverse). transition() releases the state lock before it
        # calls _handle_transition, and _on_wake/request_wake call transition()
        # while holding _lock, so _handle_transition must not take _lock.
        self._lock = threading.Lock()
        self._pending: _PendingWake | None = None
        self._session_stop: threading.Event | None = None
        self._wake_latch: tuple[bool, str | None] = (False, None)
        self._manual_rearm_reserved = False
        self._app_stop: Any = None
        self._subscription: AudioSubscription | None = None
        self._worker: WakeWordWorker | None = None
        self._last_wake: dict[str, Any] | None = None

    # -- observability -------------------------------------------------

    def _handle_transition(self, old: PresenceState, new: PresenceState, reason: str) -> None:
        # Spec §27 lists five presence.* names; we emit ONE presence.transition
        # carrying from_state/to_state (the edge, not just the arrival), matching
        # the existing fsm.transition convention. `to_state` is greppable, so no
        # information is lost — do not split this back into five events.
        self._status.record_event(
            "presence.transition",
            from_state=old.name.lower(),
            to_state=new.name.lower(),
            reason=reason,
        )
        if self._external_on_transition is not None:
            self._external_on_transition(old, new, reason)

    @property
    def state(self) -> PresenceState:
        return self._states.state

    def snapshot(self) -> dict[str, Any]:
        """Return manager observability without taking the lifecycle lock.

        Writers replace the immutable latch pair while holding ``_lock``. This
        gives the two latch fields one shared instant, while allowing transition
        observers to snapshot safely. The separately-read FSM state can lag the
        latch during an in-flight transition.
        """
        worker = self._worker
        wake_latched, wake_latch_reason = self._wake_latch
        return {
            "state": self._states.state.name.lower(),
            "wake_latched": wake_latched,
            "wake_latch_reason": wake_latch_reason,
            "wake_count": worker.wake_count if worker is not None else 0,
            "frames_total": worker.frames_total if worker is not None else 0,
            "restart_count": worker.restart_count if worker is not None else 0,
            "backend_error_count": worker.backend_error_count if worker is not None else 0,
            "seconds_since_process": (
                round(worker.seconds_since_process(), 3) if worker is not None else None
            ),
            "last_wake": self._last_wake,
        }

    # -- lifecycle -----------------------------------------------------

    def run(self, stop_event: Any) -> None:
        """Boot, then loop: while asleep the worker drives wakes; each armed
        wake runs one session to completion. Blocks until ``stop_event`` is set;
        call it on the main thread (it owns ``asyncio.run`` per session)."""
        self._app_stop = stop_event
        if self._boot_motion_enabled:
            self._motion.boot_motion()

        # Initialize the wake model before declaring SLEEPING (spec §21). The
        # detector loads inside the worker thread; _on_wake ignores any event
        # that arrives before the SLEEPING transition below.
        if self._detector is not None:
            self._subscription = self._capture.subscribe(
                "wake", max_buffer_ms=_WAKE_SUBSCRIPTION_BUFFER_MS
            )
            self._worker = WakeWordWorker(
                self._subscription,
                self._detector,
                self._ring_buffer,
                self._on_wake,
                debounce_seconds=self._debounce_seconds,
                stall_seconds=self._stall_seconds,
                clock=self._clock,
                on_event=self._status.record_event,
            )
            self._worker.start()
            self._status.record_event("wake.buffer_started")

        self._states.transition(PresenceState.SLEEPING, reason="boot_complete")
        self._motion.sleeping_pose()

        if self._detector is None:
            # No wake model: rest, stay reachable for manual wake, flag the fault.
            self._states.transition(PresenceState.ERROR, reason="wake_model_unavailable")

        try:
            while not stop_event.is_set():
                pending = self._take_pending()
                if pending is None:
                    stop_event.wait(0.1)
                    continue
                self._run_session(pending)
        finally:
            if self._worker is not None:
                self._worker.close()
            if self._subscription is not None:
                self._capture.unsubscribe("wake")
            self._worker = None
            self._subscription = None

    # -- wake arming ---------------------------------------------------

    def _on_wake(self, event: WakeEvent) -> None:
        """Worker-thread callback. Arm at most one wake, and only from SLEEPING."""
        ignored_latched = False
        with self._lock:
            if self._wake_latch[0]:
                ignored_latched = True
            elif (
                self._manual_rearm_reserved
                or self._pending is not None
                or self._states.state is not PresenceState.SLEEPING
            ):
                return
            else:
                wake_audio = self._assembler.collect(event.detected_at)
                pending = _PendingWake(
                    wake_audio=wake_audio,
                    event=event,
                    stop_event=threading.Event(),
                )
                self._pending = pending
                self._session_stop = pending.stop_event
                self._last_wake = {
                    "id": event.id,
                    "phrase": event.phrase,
                    "score": round(event.score, 3),
                }
                self._states.transition(PresenceState.WAKING, reason="wake_word")
        if ignored_latched:
            self._status.record_event("wake.ignored", reason="noise_bail_latched")
            return
        # Outside the lock: the pre-roll was flushed from history into this wake.
        self._status.record_event("wake.detected", phrase=event.phrase, score=round(event.score, 3))
        self._status.record_event("wake.buffer_flushed", frames=len(wake_audio))
        if self._wake_motion_enabled:
            self._motion.wake_acknowledge()

    def request_wake(self) -> dict[str, Any]:
        """Start a ready-gated session with no pre-roll or model greeting."""
        with self._lock:
            state = self._states.state
            if state not in (PresenceState.SLEEPING, PresenceState.ERROR):
                return {"ok": False, "state": state.name.lower(), "reason": "not_sleeping"}
            if self._pending is not None or self._manual_rearm_reserved:
                return {"ok": False, "state": state.name.lower(), "reason": "wake_in_progress"}
            latch_cleared = self._wake_latch[0]
            if latch_cleared:
                self._wake_latch = (False, None)
                self._manual_rearm_reserved = True
            else:
                pending = _PendingWake(
                    wake_audio=None,
                    event=None,
                    stop_event=threading.Event(),
                )
                self._pending = pending
                self._session_stop = pending.stop_event
                self._states.transition(PresenceState.WAKING, reason="manual_wake")
        if latch_cleared:
            self._status.set_wake_latch(False, None)
            self._status.record_event("wake.manual_rearm", reason="noise_bail")
            with self._lock:
                self._manual_rearm_reserved = False
                pending = _PendingWake(
                    wake_audio=None,
                    event=None,
                    stop_event=threading.Event(),
                )
                self._pending = pending
                self._session_stop = pending.stop_event
                self._states.transition(PresenceState.WAKING, reason="manual_wake")
        self._status.record_event("wake.manual", action="wake")
        if self._wake_motion_enabled:
            self._motion.wake_acknowledge()
        return {"ok": True, "state": "waking"}

    def request_sleep(self) -> dict[str, Any]:
        """End a starting or active session and return it to sleeping."""
        with self._lock:
            state = self._states.state
            if state not in (PresenceState.WAKING, PresenceState.AWAKE):
                return {"ok": False, "state": state.name.lower(), "reason": "not_awake"}
            if state is PresenceState.WAKING and self._pending is not None:
                self._pending.cancel_reason = "manual_sleep"
            session_stop = self._session_stop
        if session_stop is not None:
            session_stop.set()
        self._status.record_event("wake.manual", action="sleep")
        return {"ok": True, "state": "sleeping"}

    def _take_pending(self) -> _PendingWake | None:
        # _pending stays armed while its session runs (_run_session clears it in
        # its finally), so the blocking loop never re-enters the same session and
        # concurrent wakes are ignored until the current one finishes.
        with self._lock:
            return self._pending

    # -- session -------------------------------------------------------

    def _run_session(self, pending: _PendingWake) -> None:
        session_stop = pending.stop_event
        startup_resolved = threading.Event()
        deadline_expired = threading.Event()
        deadline_at = self._clock() + self._connect_timeout_seconds
        combined = _WakeStartupStop(
            self._app_stop,
            session_stop,
            lifecycle_lock=self._lock,
            startup_resolved=startup_resolved,
            deadline_expired=deadline_expired,
            deadline_at=deadline_at,
            clock=self._clock,
        )

        def _on_session_ready() -> None:
            accepted = False
            deadline_won = False
            with self._lock:
                if startup_resolved.is_set():
                    return
                if (
                    session_stop.is_set()
                    or self._app_stop.is_set()
                    or pending.cancel_reason == "manual_sleep"
                ):
                    startup_resolved.set()
                elif combined._resolve_deadline_locked():
                    deadline_won = True
                elif self._states.state is PresenceState.WAKING:
                    startup_resolved.set()
                    self._states.transition(PresenceState.AWAKE, reason="session_ready")
                    accepted = True
                else:
                    startup_resolved.set()
            if deadline_won:
                session_stop.set()
            elif accepted:
                self._status.record_event("wake.session_ready")

        session = self._session_factory(
            wake_session=True,
            on_session_ready=_on_session_ready,
        )

        def _watch_deadline() -> None:
            remaining = max(0.0, deadline_at - self._clock())
            if startup_resolved.wait(remaining):
                return
            combined.resolve_deadline()

        watchdog = threading.Thread(target=_watch_deadline, name="wake-connect-deadline", daemon=True)
        watchdog.start()

        self._status.record_event("wake.connection_start")
        outcome = SessionOutcome.STOPPED
        try:
            outcome = asyncio.run(session.run(combined))
        except Exception as error:  # a crashed session is a failed turn, not a crash of the app
            logger.exception("wake session crashed")
            self._status.record_error(f"wake session crashed: {error}")
        finally:
            combined.resolve_deadline()
            with self._lock:
                if not startup_resolved.is_set():
                    startup_resolved.set()
            session_stop.set()
            watchdog.join(timeout=1.0)
            latched = outcome is SessionOutcome.NOISE_BAIL
            failure_stage = (
                "deadline"
                if deadline_expired.is_set()
                else getattr(session, "startup_failure_stage", None)
            )
            app_stopping = self._app_stop.is_set()
            with self._lock:
                if latched:
                    self._wake_latch = (True, "noise_bail")
                cancel_reason = pending.cancel_reason
                state = self._states.state
                startup_failed = (
                    state is PresenceState.WAKING
                    and not app_stopping
                    and cancel_reason != "manual_sleep"
                )
                if state in (PresenceState.AWAKE, PresenceState.WAKING):
                    self._states.transition(
                        PresenceState.SLEEPING,
                        reason="session_ended",
                    )
                self._pending = None
                self._session_stop = None
            if latched:
                self._status.set_wake_latch(True, "noise_bail")
                self._status.record_event("presence.noise_bail_latched", reason="noise_bail")
            self._finish_session(
                app_stopping=app_stopping,
                startup_failed=startup_failed,
                failure_stage=failure_stage,
            )

    def _finish_session(
        self,
        *,
        app_stopping: bool,
        startup_failed: bool,
        failure_stage: str | None,
    ) -> None:
        if startup_failed:
            self._status.record_event("wake.connection_failed", stage=failure_stage or "startup")
            if self._wake_motion_enabled:
                self._motion.connection_failed_motion()
        if not app_stopping:
            self._motion.sleeping_pose()
