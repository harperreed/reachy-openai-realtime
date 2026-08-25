import asyncio
import contextlib
import threading
import time

import numpy as np
import pytest

from reachy_openai_realtime.audio.fanout import AudioFrame, AudioSubscription
from reachy_openai_realtime.presence.manager import PresenceManager, WakeAudioAssembler, _EitherStop
from reachy_openai_realtime.presence.states import PresenceState, PresenceStateMachine
from reachy_openai_realtime.runtime_status import RuntimeStatus
from reachy_openai_realtime.session.recovery import SessionOutcome
from reachy_openai_realtime.wakeword.base import WakeWordDetection
from reachy_openai_realtime.wakeword.buffer import AudioRingBuffer
from reachy_openai_realtime.wakeword.worker import WakeEvent


def test_starts_booting_and_boots_to_sleeping():
    fsm = PresenceStateMachine()
    assert fsm.state is PresenceState.BOOTING
    fsm.transition(PresenceState.SLEEPING, reason="boot_complete")
    assert fsm.state is PresenceState.SLEEPING


def test_wake_cycle_transitions_are_legal():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.WAKING, reason="wake_word")
    fsm.transition(PresenceState.AWAKE, reason="session_ready")
    fsm.transition(PresenceState.SLEEPING, reason="manual_sleep")


def test_startup_failure_returns_to_sleeping():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.WAKING, reason="wake_word")
    fsm.transition(PresenceState.SLEEPING, reason="startup_failure")


def test_illegal_transition_raises():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    with pytest.raises(ValueError, match="illegal presence transition"):
        fsm.transition(PresenceState.AWAKE, reason="skip_waking")


def test_self_transition_is_idempotent():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.SLEEPING, reason="already_asleep")
    assert fsm.state is PresenceState.SLEEPING


def test_on_transition_callback_fires_with_from_to_reason():
    seen: list[tuple] = []
    fsm = PresenceStateMachine(on_transition=lambda old, new, reason: seen.append((old, new, reason)))
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    assert seen == [(PresenceState.BOOTING, PresenceState.SLEEPING, "boot")]


def test_error_recovers_via_waking():
    fsm = PresenceStateMachine()
    fsm.transition(PresenceState.SLEEPING, reason="boot")
    fsm.transition(PresenceState.ERROR, reason="model_download_failed")
    fsm.transition(PresenceState.WAKING, reason="manual_wake")


def test_either_stop_set_targets_session_event_only() -> None:
    app_stop = threading.Event()
    session_stop = threading.Event()
    combined = _EitherStop(app_stop, session_stop)

    combined.set()

    assert combined.is_set() is True
    assert session_stop.is_set() is True
    assert app_stop.is_set() is False


# --- PresenceManager + WakeAudioAssembler (Task 11) ---


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _frame(value=100):
    return AudioFrame(
        samples=np.full(160, value, dtype=np.int16),
        sample_rate=16_000,
        captured_at=time.monotonic(),
    )


class FakeMotion:
    def __init__(self):
        self.calls = []

    def boot_motion(self):
        self.calls.append("boot")
        return {"ok": True, "motion": "boot_motion"}

    def sleeping_pose(self):
        self.calls.append("sleep")
        return {"ok": True, "motion": "sleeping_pose"}

    def wake_acknowledge(self):
        self.calls.append("wake")
        return {"ok": True, "motion": "wake_acknowledge"}

    def connection_failed_motion(self):
        self.calls.append("fail")
        return {"ok": True, "motion": "connection_failed_motion"}


class FakeDetector:
    """Fires on the Nth process() call (mirrors the wake-worker test fake)."""

    def __init__(self, fire_after=3):
        self.required_sample_rate = 24_000
        self._calls = 0
        self._fire_after = fire_after

    def start(self):
        pass

    def process(self, pcm16):
        self._calls += 1
        if self._calls >= self._fire_after:
            return WakeWordDetection(phrase="hey reachy", score=0.95, detected_at=time.monotonic())
        return None

    def reset(self):
        self._calls = 0

    def close(self):
        pass


class FakeCapture:
    """Hands out real AudioSubscriptions the test feeds by hand; the manager
    only calls subscribe()/unsubscribe(), never a live media device."""

    def __init__(self):
        self.subscriptions = {}

    def subscribe(self, name, *, max_buffer_ms=None):
        sub = AudioSubscription(name, max_buffer_ms=max_buffer_ms or 2_000.0)
        self.subscriptions[name] = sub
        return sub

    def unsubscribe(self, name):
        self.subscriptions.pop(name, None)

    def feed(self, frame):
        self.subscriptions["wake"]._offer(frame)


class FakeStatus:
    def __init__(self):
        self.events = []
        self.errors = []
        self.wake_latches: list[tuple[bool, str | None]] = []

    def record_event(self, event, **fields):
        self.events.append((event, fields))

    def record_error(self, error):
        self.errors.append(error)

    def set_wake_latch(self, latched: bool, reason: str | None) -> None:
        self.wake_latches.append((latched, reason))


class FakeSession:
    startup_failure_stage: str | None = None

    def __init__(self, *, wake_session=False, on_session_ready=None, outcome=SessionOutcome.STOPPED):
        self.wake_session = wake_session
        self._on_session_ready = on_session_ready
        self.outcome = outcome
        self.ran = False

    async def run(self, stop_event):
        self.ran = True
        if self._on_session_ready is not None:
            self._on_session_ready()
        if self.outcome is SessionOutcome.NOISE_BAIL:
            return self.outcome
        while not stop_event.is_set():
            await asyncio.sleep(0.01)
        return self.outcome


class SessionRecorder:
    """A session_factory stand-in. connect=False withholds the ready callback
    so the connect deadline fires (the failed-connection path, spec §20)."""

    def __init__(self, *, connect=True, outcomes: list[SessionOutcome] | None = None):
        self.sessions = []
        self._connect = connect
        self._outcomes = list(outcomes or [])

    def __call__(self, *, wake_session=False, on_session_ready=None):
        outcome = self._outcomes.pop(0) if self._outcomes else SessionOutcome.STOPPED
        session = FakeSession(
            wake_session=wake_session,
            on_session_ready=on_session_ready if self._connect else None,
            outcome=outcome,
        )
        self.sessions.append(session)
        return session


class GatedNoiseBailSession:
    startup_failure_stage: str | None = None

    def __init__(self, *, wake_session=False, on_session_ready=None, return_gate=None):
        self.wake_session = wake_session
        self._on_session_ready = on_session_ready
        self._return_gate = return_gate

    async def run(self, stop_event):
        if self._on_session_ready is not None:
            self._on_session_ready()
        if self._return_gate is not None:
            await asyncio.to_thread(self._return_gate.wait)
        return SessionOutcome.NOISE_BAIL


class RearmOrderingStatus(RuntimeStatus):
    """Coordinates the old stale-publication window without timing sleeps."""

    def __init__(self) -> None:
        super().__init__()
        self.manager: PresenceManager | None = None
        self.events: list[tuple[str, dict[str, object]]] = []
        self.false_publication_started = threading.Event()
        self.second_noise_bail_published = threading.Event()
        self._noise_bail_publications = 0

    def record_event(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))
        super().record_event(event, **fields)

    def set_wake_latch(self, latched: bool, reason: str | None) -> None:
        if not latched:
            self.false_publication_started.set()
            if self.manager is not None and self.manager.state is PresenceState.WAKING:
                assert self.second_noise_bail_published.wait(timeout=3.0)
        super().set_wake_latch(latched, reason)
        if latched and reason == "noise_bail":
            self._noise_bail_publications += 1
            if self._noise_bail_publications == 2:
                self.second_noise_bail_published.set()


@contextlib.contextmanager
def _running(manager):
    stop = threading.Event()
    thread = threading.Thread(target=manager.run, args=(stop,), name="presence-test", daemon=True)
    thread.start()
    try:
        yield stop
    finally:
        stop.set()
        thread.join(timeout=3.0)
        assert not thread.is_alive(), "PresenceManager.run() did not stop within 3 s"


def test_wake_audio_assembler_returns_preroll_before_detection():
    ring = AudioRingBuffer(history_seconds=10.0)
    for captured_at in (1.0, 1.5, 1.9, 2.0, 2.5):
        ring.append(
            AudioFrame(samples=np.zeros(160, dtype=np.int16), sample_rate=16_000, captured_at=captured_at)
        )
    assembler = WakeAudioAssembler(ring, pre_roll_seconds=0.4)

    frames = assembler.collect(detected_at=2.0)  # cutoff 1.6 → 1.9, 2.0, 2.5

    assert [f.captured_at for f in frames] == [1.9, 2.0, 2.5]


def test_boot_reaches_sleeping_and_plays_boot_then_sleep_pose():
    motion = FakeMotion()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=motion,
        session_factory=SessionRecorder(),
        status=FakeStatus(),
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
    assert motion.calls[:2] == ["boot", "sleep"]


def test_wake_word_builds_ready_gated_session_without_preroll() -> None:
    capture = FakeCapture()
    motion = FakeMotion()
    factory = SessionRecorder(connect=True)
    status = FakeStatus()
    manager = PresenceManager(
        capture=capture,
        detector=FakeDetector(fire_after=3),
        motion=motion,
        session_factory=factory,
        status=status,
        pre_roll_seconds=1.0,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        for _ in range(5):
            capture.feed(_frame())
            time.sleep(0.02)
        assert _wait_until(lambda: manager.state is PresenceState.AWAKE)

    assert "wake" in motion.calls
    assert len(factory.sessions) == 1
    assert factory.sessions[0].wake_session is True
    assert not hasattr(factory.sessions[0], "pending_wake_audio")
    kinds = [event for event, _ in status.events]
    assert "wake.detected" in kinds  # spec §27 structured logging
    assert "wake.session_ready" in kinds


def test_manual_wake_builds_session_without_preroll():
    factory = SessionRecorder(connect=True)
    status = FakeStatus()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=factory,
        status=status,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.AWAKE)

    assert len(factory.sessions) == 1
    assert factory.sessions[0].wake_session is True
    assert not hasattr(factory.sessions[0], "pending_wake_audio")
    assert ("wake.manual", {"action": "wake"}) in status.events  # spec §27


def test_noise_bail_latches_wake_word_until_manual_wake() -> None:
    capture = FakeCapture()
    factory = SessionRecorder(
        connect=True,
        outcomes=[SessionOutcome.NOISE_BAIL, SessionOutcome.STOPPED],
    )
    status = FakeStatus()
    manager = PresenceManager(
        capture=capture,
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=factory,
        status=status,
    )

    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: manager.snapshot()["wake_latched"] is True)
        assert manager.snapshot()["wake_latch_reason"] == "noise_bail"
        assert manager.state is PresenceState.SLEEPING

        manager._on_wake(
            WakeEvent(
                id="ignored",
                detected_at=time.monotonic(),
                phrase="hey reachy",
                score=0.99,
            )
        )
        assert _wait_until(
            lambda: ("wake.ignored", {"reason": "noise_bail_latched"}) in status.events
        )
        assert len(factory.sessions) == 1

        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: len(factory.sessions) == 2)
        assert manager.snapshot()["wake_latched"] is False
        assert manager.snapshot()["wake_latch_reason"] is None


def test_manual_rearm_cannot_publish_stale_clear_after_second_noise_bail() -> None:
    second_bail_gate = threading.Event()
    status = RearmOrderingStatus()
    sessions: list[GatedNoiseBailSession] = []

    def session_factory(*, wake_session=False, on_session_ready=None):
        session = GatedNoiseBailSession(
            wake_session=wake_session,
            on_session_ready=on_session_ready,
            return_gate=second_bail_gate if sessions else None,
        )
        sessions.append(session)
        return session

    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=session_factory,
        status=status,
    )
    status.manager = manager

    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: manager.snapshot()["wake_latched"] is True)
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)

        result: list[dict[str, object]] = []
        rearm_thread = threading.Thread(
            target=lambda: result.append(manager.request_wake()),
            name="manual-rearm-test",
        )
        rearm_thread.start()
        assert status.false_publication_started.wait(timeout=3.0)
        second_bail_gate.set()
        rearm_thread.join(timeout=3.0)
        assert not rearm_thread.is_alive(), "manual wake did not return"
        assert result == [{"ok": True, "state": "waking"}]
        assert status.second_noise_bail_published.wait(timeout=3.0)

        manager_latch = manager.snapshot()
        status_latch = status.snapshot()
        assert (manager_latch["wake_latched"], manager_latch["wake_latch_reason"]) == (
            True,
            "noise_bail",
        )
        assert (status_latch["wake_latched"], status_latch["wake_latch_reason"]) == (
            True,
            "noise_bail",
        )
        manager._on_wake(
            WakeEvent(
                id="ignored-after-second-bail",
                detected_at=time.monotonic(),
                phrase="hey reachy",
                score=0.99,
            )
        )
        assert ("wake.ignored", {"reason": "noise_bail_latched"}) in status.events


def test_transition_observer_can_snapshot_during_wake() -> None:
    observed: list[dict[str, object]] = []
    callback_completed = threading.Event()
    manager_holder: dict[str, PresenceManager] = {}

    def observer(old, new, reason) -> None:
        if reason == "wake_word":
            observed.append(manager_holder["manager"].snapshot())
            callback_completed.set()

    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=SessionRecorder(),
        status=FakeStatus(),
        on_transition=observer,
    )
    manager_holder["manager"] = manager
    manager._states.transition(PresenceState.SLEEPING, reason="boot_complete")
    callback_thread = threading.Thread(
        target=lambda: manager._on_wake(
            WakeEvent(
                id="callback-snapshot",
                detected_at=time.monotonic(),
                phrase="hey reachy",
                score=0.99,
            )
        ),
        name="wake-callback-test",
        daemon=True,
    )

    callback_thread.start()
    callback_thread.join(timeout=0.3)

    assert not callback_thread.is_alive(), "transition observer deadlocked on manager.snapshot()"
    assert callback_completed.is_set()
    assert (observed[-1]["wake_latched"], observed[-1]["wake_latch_reason"]) == (False, None)


def test_manual_sleep_ends_active_session():
    factory = SessionRecorder(connect=True)
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=factory,
        status=FakeStatus(),
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        manager.request_wake()
        assert _wait_until(lambda: manager.state is PresenceState.AWAKE)
        assert manager.request_sleep()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)

    assert factory.sessions[0].ran is True


def test_manual_sleep_leaves_wake_unlatched():
    factory = SessionRecorder(connect=True)
    status = FakeStatus()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=factory,
        status=status,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.AWAKE)
        assert manager.request_sleep()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.snapshot()["wake_latched"] is False

    assert status.wake_latches == []


def test_request_sleep_while_asleep_is_rejected():
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=SessionRecorder(),
        status=FakeStatus(),
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        result = manager.request_sleep()
    assert result == {"ok": False, "state": "sleeping", "reason": "not_awake"}


def test_missing_detector_boots_to_error_but_manual_wake_works():
    factory = SessionRecorder(connect=True)
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=None,
        motion=FakeMotion(),
        session_factory=factory,
        status=FakeStatus(),
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.ERROR)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.AWAKE)

    assert len(factory.sessions) == 1


def test_failed_connection_returns_to_sleeping_with_failure_motion():
    motion = FakeMotion()
    factory = SessionRecorder(connect=False)
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=motion,
        session_factory=factory,
        status=FakeStatus(),
        connect_timeout_seconds=0.3,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        manager.request_wake()
        assert _wait_until(lambda: "fail" in motion.calls, timeout=3.0)
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)

    assert factory.sessions[0].ran is True
    assert manager.snapshot()["state"] == "sleeping"


def test_failed_connection_leaves_wake_unlatched():
    factory = SessionRecorder(connect=False)
    status = FakeStatus()
    motion = FakeMotion()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=motion,
        session_factory=factory,
        status=status,
        connect_timeout_seconds=0.3,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: "fail" in motion.calls)
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.snapshot()["wake_latched"] is False

    assert status.wake_latches == []


def test_ready_beep_failure_stage_is_recorded() -> None:
    class FailedReadyBeepSession:
        startup_failure_stage = "ready_beep_write"

        async def run(self, stop_event):
            return SessionOutcome.STOPPED

    status = FakeStatus()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=lambda **_kwargs: FailedReadyBeepSession(),
        status=status,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(
            lambda: (
                "wake.connection_failed",
                {"stage": "ready_beep_write"},
            )
            in status.events
        )


def test_manual_sleep_cancels_session_while_waking() -> None:
    started = threading.Event()

    class WakingSession:
        startup_failure_stage = None

        async def run(self, stop_event):
            started.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.01)
            return SessionOutcome.STOPPED

    status = FakeStatus()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=lambda **_kwargs: WakingSession(),
        status=status,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert started.wait(timeout=1.0)
        assert manager.state is PresenceState.WAKING
        assert manager.request_sleep()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
    assert not any(event == "wake.connection_failed" for event, _fields in status.events)


def test_ready_callback_cannot_publish_awake_after_manual_cancel_starts() -> None:
    started = threading.Event()
    release_ready = threading.Event()
    ready_returned = threading.Event()

    class BlockingStopEvent:
        def __init__(self) -> None:
            self._event = threading.Event()
            self.set_started = threading.Event()
            self.allow_set = threading.Event()

        def is_set(self) -> bool:
            return self._event.is_set()

        def set(self) -> None:
            self.set_started.set()
            assert self.allow_set.wait(timeout=1.0)
            self._event.set()

    class LateReadySession:
        startup_failure_stage = None

        def __init__(self, *, on_session_ready) -> None:
            self._on_session_ready = on_session_ready

        async def run(self, stop_event):
            started.set()
            await asyncio.to_thread(release_ready.wait)
            self._on_session_ready()
            ready_returned.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.01)
            return SessionOutcome.STOPPED

    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=lambda **kwargs: LateReadySession(
            on_session_ready=kwargs["on_session_ready"]
        ),
        status=FakeStatus(),
    )
    manager._app_stop = threading.Event()
    manager._states.transition(PresenceState.SLEEPING, reason="boot_complete")
    assert manager.request_wake()["ok"] is True

    with manager._lock:
        pending = manager._pending
        assert pending is not None
        stop_event = BlockingStopEvent()
        pending.stop_event = stop_event
        manager._session_stop = stop_event

    session_thread = threading.Thread(target=manager._run_session, args=(pending,), daemon=True)
    session_thread.start()
    assert started.wait(timeout=1.0)

    sleep_thread = threading.Thread(target=manager.request_sleep, daemon=True)
    sleep_thread.start()
    assert stop_event.set_started.wait(timeout=1.0)
    release_ready.set()
    assert ready_returned.wait(timeout=1.0)
    assert manager.state is PresenceState.WAKING

    stop_event.allow_set.set()
    sleep_thread.join(timeout=1.0)
    session_thread.join(timeout=1.0)
    assert not sleep_thread.is_alive()
    assert not session_thread.is_alive()


def test_ready_callback_rejects_app_stop_after_session_last_check() -> None:
    started = threading.Event()
    release_ready = threading.Event()

    class AppStoppingSession:
        startup_failure_stage = None

        def __init__(self, *, on_session_ready) -> None:
            self._on_session_ready = on_session_ready

        async def run(self, stop_event):
            assert stop_event.is_set() is False
            started.set()
            await asyncio.to_thread(release_ready.wait)
            self._on_session_ready()
            return SessionOutcome.STOPPED

    status = FakeStatus()
    motion = FakeMotion()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=motion,
        session_factory=lambda **kwargs: AppStoppingSession(
            on_session_ready=kwargs["on_session_ready"]
        ),
        status=status,
    )
    app_stop = threading.Event()
    manager._app_stop = app_stop
    manager._states.transition(PresenceState.SLEEPING, reason="boot_complete")
    assert manager.request_wake()["ok"] is True
    pending = manager._pending
    assert pending is not None

    session_thread = threading.Thread(target=manager._run_session, args=(pending,), daemon=True)
    session_thread.start()
    assert started.wait(timeout=1.0)
    app_stop.set()
    release_ready.set()
    session_thread.join(timeout=1.0)

    assert not session_thread.is_alive()
    assert manager.state is PresenceState.SLEEPING
    assert not any(event == "wake.session_ready" for event, _fields in status.events)
    assert not any(
        event == "presence.transition" and fields.get("to_state") == "awake"
        for event, fields in status.events
    )
    assert not any(event == "wake.connection_failed" for event, _fields in status.events)
    assert "fail" not in motion.calls


def test_deadline_wins_when_ready_callback_arrives_at_deadline() -> None:
    now = [100.0]
    callback_poised = threading.Event()
    release_ready = threading.Event()

    class DeadlineReadySession:
        startup_failure_stage = None

        def __init__(self, *, on_session_ready) -> None:
            self._on_session_ready = on_session_ready

        async def run(self, stop_event):
            assert stop_event.is_set() is False
            callback_poised.set()
            await asyncio.to_thread(release_ready.wait)
            self._on_session_ready()
            return SessionOutcome.STOPPED

    status = FakeStatus()
    motion = FakeMotion()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=motion,
        session_factory=lambda **kwargs: DeadlineReadySession(
            on_session_ready=kwargs["on_session_ready"]
        ),
        status=status,
        connect_timeout_seconds=10.0,
        clock=lambda: now[0],
    )
    manager._app_stop = threading.Event()
    manager._states.transition(PresenceState.SLEEPING, reason="boot_complete")
    assert manager.request_wake()["ok"] is True
    pending = manager._pending
    assert pending is not None

    session_thread = threading.Thread(target=manager._run_session, args=(pending,), daemon=True)
    session_thread.start()
    assert callback_poised.wait(timeout=1.0)
    now[0] = 110.0
    release_ready.set()
    session_thread.join(timeout=1.0)

    assert not session_thread.is_alive()
    assert manager.state is PresenceState.SLEEPING
    assert not any(event == "wake.session_ready" for event, _fields in status.events)
    assert status.events.count(("wake.connection_failed", {"stage": "deadline"})) == 1
    assert motion.calls.count("fail") == 1


def test_session_exception_leaves_wake_unlatched():
    class RaisingSession:
        async def run(self, stop_event):
            raise RuntimeError("test session failure")

    status = FakeStatus()
    manager = PresenceManager(
        capture=FakeCapture(),
        detector=FakeDetector(fire_after=10_000),
        motion=FakeMotion(),
        session_factory=lambda **_kwargs: RaisingSession(),
        status=status,
    )
    with _running(manager):
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        assert manager.request_wake()["ok"] is True
        assert _wait_until(lambda: manager.state is PresenceState.SLEEPING)
        snapshot = manager.snapshot()
        assert (snapshot["wake_latched"], snapshot["wake_latch_reason"]) == (False, None)

    assert status.wake_latches == []
