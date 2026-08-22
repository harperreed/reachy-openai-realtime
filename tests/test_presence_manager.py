import asyncio
import contextlib
import threading
import time

import numpy as np
import pytest

from reachy_openai_realtime.audio.fanout import AudioFrame, AudioSubscription
from reachy_openai_realtime.presence.manager import PresenceManager, WakeAudioAssembler
from reachy_openai_realtime.presence.states import PresenceState, PresenceStateMachine
from reachy_openai_realtime.wakeword.base import WakeWordDetection
from reachy_openai_realtime.wakeword.buffer import AudioRingBuffer


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

    def record_event(self, event, **fields):
        self.events.append((event, fields))

    def record_error(self, error):
        self.errors.append(error)


class FakeSession:
    def __init__(self, *, pending_wake_audio=None, on_session_ready=None):
        self.pending_wake_audio = pending_wake_audio
        self._on_session_ready = on_session_ready
        self.ran = False

    async def run(self, stop_event):
        self.ran = True
        if self._on_session_ready is not None:
            self._on_session_ready()
        while not stop_event.is_set():
            await asyncio.sleep(0.01)
        return "STOPPED"


class SessionRecorder:
    """A session_factory stand-in. connect=False withholds the ready callback
    so the connect deadline fires (the failed-connection path, spec §20)."""

    def __init__(self, *, connect=True):
        self.sessions = []
        self._connect = connect

    def __call__(self, *, pending_wake_audio=None, on_session_ready=None):
        session = FakeSession(
            pending_wake_audio=pending_wake_audio,
            on_session_ready=on_session_ready if self._connect else None,
        )
        self.sessions.append(session)
        return session


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


def test_wake_word_reaches_awake_and_seeds_session_with_preroll():
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
    assert factory.sessions[0].pending_wake_audio  # non-empty raw pre-roll
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
    assert factory.sessions[0].pending_wake_audio is None
    assert ("wake.manual", {"action": "wake"}) in status.events  # spec §27


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
