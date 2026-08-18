# ABOUTME: Integration harness for main.ReachyOpenaiRealtime.run() outer loop (issue #3).
# ABOUTME: Drives the loop through an AudioPipelineStalled then a clean stop; no real sessions.
from __future__ import annotations

import threading
from typing import Any

from conftest import FakeRecorder
from reachy_mini.utils import create_head_pose

from reachy_openai_realtime.audio.capture import AudioPipelineStalled
from reachy_openai_realtime.main import ReachyOpenaiRealtime
from reachy_openai_realtime.session.recovery import SessionOutcome
from reachy_openai_realtime.session.supervisor import RestartBudget

# ---------------------------------------------------------------------------
# Fake robot surface
# ---------------------------------------------------------------------------


class FakeMedia:
    """Media stub that satisfies main.run() without touching real hardware."""

    def __init__(self) -> None:
        self.camera = None  # no camera available
        self.calls: list[str] = []

    def start_recording(self) -> None:
        self.calls.append("start_recording")

    def stop_recording(self) -> None:
        self.calls.append("stop_recording")

    def start_playing(self) -> None:
        self.calls.append("start_playing")

    def stop_playing(self) -> None:
        self.calls.append("stop_playing")

    # audio sub-object: apply_wireless_conversation_audio_config uses getattr safely
    # so omitting it is fine (returns False = current settings path).


class FakeRobot:
    """Minimal robot that satisfies both main.run() and MotionController's ReachyMotionAPI."""

    def __init__(self) -> None:
        self.media = FakeMedia()

    # MotionController protocol ------------------------------------------------

    def get_current_head_pose(self) -> Any:
        return create_head_pose(0, 0, 0, 0, 0, 0, degrees=True)

    def get_current_joint_positions(self) -> tuple[list[float], list[float]]:
        return ([0.0] * 7, [0.0, 0.0])

    def set_target(self, head: Any = None, antennas: Any = None, body_yaw: float | None = None) -> None:
        pass

    def goto_target(
        self, head: Any = None, antennas: Any = None, duration: float = 0.5, body_yaw: float | None = 0.0
    ) -> None:
        pass

    def cancel_move(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Stub session class
# ---------------------------------------------------------------------------


def make_stub_session_class(stop_event: threading.Event) -> type:
    """Return a stub RealtimeRobotSession class.

    First construction: run() raises AudioPipelineStalled.
    Second construction: run() sets stop_event and returns STOPPED.
    """
    state = {"count": 0}

    class StubRealtimeRobotSession:
        def __init__(self, robot: Any, motion: Any, config: Any, status: Any, **kwargs: Any) -> None:
            state["count"] += 1
            self._construction = state["count"]

        async def run(self, stop_event_arg: Any) -> Any:
            if self._construction == 1:
                raise AudioPipelineStalled("test stall")
            # Second session: signal stop and return cleanly.
            stop_event.set()
            return SessionOutcome.STOPPED

    return StubRealtimeRobotSession


# ---------------------------------------------------------------------------
# 9c: main.run() outer-loop harness
# ---------------------------------------------------------------------------


def test_app_loop_continues_after_audio_pipeline_stalled(tmp_path, monkeypatch) -> None:
    """Drive ReachyOpenaiRealtime.run() through a full AudioPipelineStalled cycle.

    Asserts:
    1. A status.snapshot()["events"] entry with level == "warning" exists
       (the main.py AudioPipelineStalled handler emits one).
    2. The media re-init sequence ran between the two sessions (stop/start calls
       in FakeMedia.calls between the stall and the second session).
    3. A second session WAS constructed (the loop continued after the stall).
    4. With a FakeRecorder attached: app.start and app.stop fired.
    """
    # --- environment setup ---------------------------------------------------
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-approop")

    # --- app object ----------------------------------------------------------
    app = ReachyOpenaiRealtime()
    stop_event = threading.Event()

    # main.run() does:
    #   recorder = EventRecorder(events_path())
    #   self.runtime_status.attach_recorder(recorder)
    #   recorder.record("app.start")   ← direct call, not via runtime_status
    #   ...
    #   recorder.record("app.stop")    ← same
    #   recorder.close()
    # We replace EventRecorder with FakeRecorder so our instance IS the recorder
    # object that run() holds, capturing both the direct calls and the indirect ones.
    fake_recorder = FakeRecorder()
    fake_recorder.close = lambda: None  # type: ignore[method-assign]  # run() calls close()

    monkeypatch.setattr(
        "reachy_openai_realtime.main.EventRecorder",
        lambda *_args, **_kwargs: fake_recorder,
    )

    # --- stub session --------------------------------------------------------
    StubSession = make_stub_session_class(stop_event)
    monkeypatch.setattr("reachy_openai_realtime.main.RealtimeRobotSession", StubSession)

    # --- collapse the 1-second audio warmup ----------------------------------
    # main.run() sleeps for up to 1 s after start_recording()/start_playing().
    # Monkeypatch time.sleep so warmup_remaining never blocks.
    monkeypatch.setattr("reachy_openai_realtime.main.time.sleep", lambda _: None)

    # --- run in a bounded thread ---------------------------------------------
    robot = FakeRobot()
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            app.run(robot, stop_event)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_run, name="app-loop-test", daemon=True)
    t.start()
    t.join(timeout=10.0)

    assert not t.is_alive(), "app.run() did not finish within 10 s — possible hang"
    assert not errors, f"app.run() raised unexpectedly: {errors}"

    # --- assertion 1: warning-level event from AudioPipelineStalled handler --
    snapshot = app.runtime_status.snapshot()
    warning_events = [e for e in snapshot["events"] if e.get("level") == "warning"]
    stall_warnings = [
        e for e in warning_events if "audio pipeline stalled" in e.get("message", "").lower()
    ]
    assert stall_warnings, (
        f"Expected a warning-level 'audio pipeline stalled' event; events: {snapshot['events']}"
    )

    # --- assertion 2: media re-init ran between sessions ---------------------
    # After AudioPipelineStalled: stop_playing → stop_recording → start_recording → start_playing.
    # Before any session: start_recording + start_playing (initial audio setup).
    # After the stall the re-init appends stop_playing, stop_recording, start_recording, start_playing.
    calls = robot.media.calls
    assert "stop_playing" in calls, f"stop_playing not called during re-init; calls={calls}"
    assert "stop_recording" in calls, f"stop_recording not called during re-init; calls={calls}"
    # Re-init must come after the first start_recording (initial setup).
    first_start = calls.index("start_recording")
    stop_after_start = [i for i, c in enumerate(calls) if c == "stop_recording" and i > first_start]
    assert stop_after_start, (
        f"stop_recording did not appear after initial start_recording; calls={calls}"
    )

    # --- assertion 3: second session was constructed -------------------------
    # StubSession.state is local; test via the stop_event being set (only the
    # second session sets it) and via the absence of errors.
    assert stop_event.is_set(), "stop_event never set — second session may not have been created"

    # --- assertion 4: app.start and app.stop in FakeRecorder -----------------
    recorded_names = [e for e, _ in fake_recorder.events]
    assert "app.start" in recorded_names, (
        f"app.start not recorded by FakeRecorder; got {recorded_names}"
    )
    assert "app.stop" in recorded_names, (
        f"app.stop not recorded by FakeRecorder; got {recorded_names}"
    )


# ---------------------------------------------------------------------------
# Step 7: Escalation test (spec §24)
# ---------------------------------------------------------------------------


def make_repeated_stall_session_class(stop_event: threading.Event, stall_count: int = 3) -> type:
    """Session stub that raises AudioPipelineStalled `stall_count` times, then stops cleanly."""
    state = {"count": 0}

    class StubSession:
        def __init__(self, robot: Any, motion: Any, config: Any, status: Any, **kwargs: Any) -> None:
            state["count"] += 1
            self._n = state["count"]

        async def run(self, stop_event_arg: Any) -> Any:
            if self._n <= stall_count:
                raise AudioPipelineStalled(f"stall #{self._n}")
            stop_event.set()
            return SessionOutcome.STOPPED

    return StubSession


def test_escalation_fires_and_loop_honors_stop_event(tmp_path, monkeypatch) -> None:
    """When AudioPipelineStalled hits the restart limit, supervisor.escalated is recorded
    and the loop still exits promptly when stop_event is set.
    """
    # --- environment setup ---------------------------------------------------
    monkeypatch.setenv("REACHY_OPENAI_REALTIME_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-escalation")

    # --- collapse real waits -------------------------------------------------
    # ESCALATION_PAUSE_SECONDS: patch in main's namespace so stop_event.wait() returns fast.
    monkeypatch.setattr("reachy_openai_realtime.main.ESCALATION_PAUSE_SECONDS", 0.0)
    monkeypatch.setattr("reachy_openai_realtime.main.time.sleep", lambda _: None)

    # Inject a RestartBudget with limit=2 so escalation fires on the 2nd stall.
    low_budget = RestartBudget(limit=2, window_seconds=600.0)
    monkeypatch.setattr("reachy_openai_realtime.main.RestartBudget", lambda: low_budget)

    # --- app object ----------------------------------------------------------
    app = ReachyOpenaiRealtime()
    stop_event = threading.Event()

    fake_recorder = FakeRecorder()
    fake_recorder.close = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        "reachy_openai_realtime.main.EventRecorder",
        lambda *_args, **_kwargs: fake_recorder,
    )

    # 2 stalls → escalation fires on stall #2; 3rd construction stops cleanly.
    StubSession = make_repeated_stall_session_class(stop_event, stall_count=2)
    monkeypatch.setattr("reachy_openai_realtime.main.RealtimeRobotSession", StubSession)

    # --- run in a bounded thread ---------------------------------------------
    robot = FakeRobot()
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            app.run(robot, stop_event)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=_run, name="escalation-test", daemon=True)
    t.start()
    t.join(timeout=10.0)

    assert not t.is_alive(), "app.run() did not finish within 10 s — possible hang"
    assert not errors, f"app.run() raised unexpectedly: {errors}"

    # --- assertion: supervisor.escalated was recorded ------------------------
    recorded_names = [e for e, _ in fake_recorder.events]
    assert "supervisor.escalated" in recorded_names, (
        f"Expected supervisor.escalated in events; got {recorded_names}"
    )

    # --- assertion: event reflects the INJECTED budget config (limit=2, window=600.0) ---
    escalated_fields = next(f for e, f in fake_recorder.events if e == "supervisor.escalated")
    assert escalated_fields["restarts"] == 2, (
        f"Expected restarts=2 (injected limit), got {escalated_fields['restarts']}"
    )
    assert escalated_fields["window_seconds"] == 600.0, (
        f"Expected window_seconds=600.0 (injected window), got {escalated_fields['window_seconds']}"
    )

    # --- assertion: stop_event was honored (loop exited) --------------------
    assert stop_event.is_set(), "stop_event was never set — loop may not have continued past escalation"
