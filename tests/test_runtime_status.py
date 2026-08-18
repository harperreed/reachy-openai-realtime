import json

from reachy_openai_realtime.observability.events import EventRecorder
from reachy_openai_realtime.runtime_status import RuntimeStatus, safe_message


def test_add_event_mirrors_into_recorder(tmp_path) -> None:
    from reachy_openai_realtime.observability.events import EventRecorder
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    recorder = EventRecorder(tmp_path / "events.jsonl")
    status = RuntimeStatus()
    status.attach_recorder(recorder)
    # NOTE: add_event(message, level) — level is second; brief had args swapped (typo),
    # intent is: message with a secret, level="info".
    status.add_event("connection ready sk-test-proj-abcdef1234567890", "info")
    recorder.close()

    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    mirrored = [entry for entry in lines if entry["event"] == "status.message"]
    assert mirrored
    assert "sk-***" in mirrored[0]["message"]
    assert "sk-test-proj-abcdef1234567890" not in json.dumps(lines)


def test_snapshot_includes_metrics() -> None:
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    status = RuntimeStatus()
    status.metrics.increment("reconnect_count")
    snapshot = status.snapshot()
    assert snapshot["metrics"]["counters"]["reconnect_count"] == 1


def test_status_without_recorder_still_works() -> None:
    from reachy_openai_realtime.runtime_status import RuntimeStatus

    status = RuntimeStatus()
    status.add_event("no recorder attached")  # must not raise
    assert status.snapshot()["metrics"]["counters"] == {}


def test_snapshot_tracks_runtime_activity() -> None:
    status = RuntimeStatus()
    status.set_phase("listening", "日本語で話しかけてください", connected=True, event=True)
    status.record_transcript("user", "こんにちは")
    status.record_transcript("assistant", "こんにちは！")
    status.record_motion("nod", {"count": 1}, True)
    status.record_mic_level(-31.24)
    status.record_audio_output_received()
    status.record_audio_output_played()
    status.record_interruption(1250)

    snapshot = status.snapshot()
    assert snapshot["phase"] == "listening"
    assert snapshot["connected"] is True
    assert snapshot["last_user"] == "こんにちは"
    assert snapshot["last_assistant"] == "こんにちは！"
    assert snapshot["last_motion"].startswith("nod")
    assert snapshot["mic_dbfs"] == -31.2
    assert snapshot["audio_output_chunks_received"] == 1
    assert snapshot["audio_output_chunks_played"] == 1
    assert snapshot["interruptions"] == 1
    assert "再生済み 1250ms" in snapshot["events"][0]["message"]


def test_secrets_are_redacted_from_errors_and_events() -> None:
    key = "sk-test-secret-key-abcdefghijklmnopqrstuvwxyz"
    status = RuntimeStatus()
    status.record_error(f"Authorization failed for {key}")

    snapshot = status.snapshot()
    assert key not in str(snapshot)
    assert "sk-***" in snapshot["last_error"]
    assert key not in safe_message(f"bad {key}")


def test_set_phase_emits_recorder_event_with_correct_connected(tmp_path) -> None:
    # A2: connected is snapshotted under the lock before record_event fires,
    # so the emitted value reflects the new connected state, not a stale read.
    recorder = EventRecorder(tmp_path / "events.jsonl")
    status = RuntimeStatus()
    status.attach_recorder(recorder)
    status.set_phase("listening", "ready", connected=True, detail_key="detail_listening")
    recorder.close()

    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    phase_events = [e for e in lines if e["event"] == "status.phase"]
    assert phase_events, "status.phase event was not emitted"
    assert phase_events[0]["connected"] is True
    assert phase_events[0]["phase"] == "listening"


def test_record_error_emits_recorder_event(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    status = RuntimeStatus()
    status.attach_recorder(recorder)
    status.record_error("network timeout")
    recorder.close()

    lines = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    error_events = [e for e in lines if e["event"] == "status.error"]
    assert error_events, "status.error event was not emitted"
    assert "network timeout" in error_events[0]["message"]


def test_health_defaults_unhealthy() -> None:
    status = RuntimeStatus()
    assert status.health(now=100.0) == {
        "ok": False, "realtime": False, "microphone": False,
        "speaker": False, "motion": False, "camera": False,
    }


def test_health_ok_tracks_critical_components_only() -> None:
    status = RuntimeStatus()
    status.set_phase("connected", "ok", connected=True)
    status.set_component_health("microphone", True, now=100.0)
    status.set_component_health("speaker", True, now=100.0)
    health = status.health(now=101.0)
    assert health["ok"] is True
    assert health["motion"] is False and health["camera"] is False  # reported, not gating


def test_health_periodic_component_goes_stale_after_10s() -> None:
    status = RuntimeStatus()
    status.set_phase("connected", "ok", connected=True)
    status.set_component_health("microphone", True, now=100.0)
    status.set_component_health("speaker", True, now=100.0)
    assert status.health(now=109.0)["ok"] is True
    stale = status.health(now=111.0)
    assert stale["microphone"] is False and stale["ok"] is False


def test_health_static_component_never_expires() -> None:
    status = RuntimeStatus()
    status.set_component_health("motion", True, expires=False, now=100.0)
    assert status.health(now=10_000.0)["motion"] is True


def test_snapshot_includes_cumulative_response_usage() -> None:
    status = RuntimeStatus()
    status.record_usage(
        "gpt-realtime-2.1",
        {
            "total_tokens": 30,
            "input_tokens": 20,
            "output_tokens": 10,
            "input_token_details": {
                "text_tokens": 10,
                "audio_tokens": 10,
                "image_tokens": 0,
                "cached_tokens": 5,
                "cached_tokens_details": {
                    "text_tokens": 5,
                    "audio_tokens": 0,
                    "image_tokens": 0,
                },
            },
            "output_token_details": {"text_tokens": 4, "audio_tokens": 6},
        },
    )

    snapshot = status.snapshot()
    assert snapshot["usage"]["lifetime"]["total_tokens"] == 30
    assert snapshot["usage"]["lifetime"]["input_tokens"] == 20
    assert snapshot["usage"]["lifetime"]["output_tokens"] == 10
    assert snapshot["usage"]["lifetime"]["estimated_cost_usd"] > 0
    assert snapshot["events"][0]["key"] == "event_usage_recorded"
