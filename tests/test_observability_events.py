import json
import threading

from reachy_openai_realtime.observability.events import EventRecorder, redact_secrets


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_redact_secrets_masks_api_keys() -> None:
    assert redact_secrets("key sk-test-proj-abcdef1234567890 leaked") == "key sk-*** leaked"
    assert redact_secrets("no secrets here") == "no secrets here"


def test_record_writes_enriched_jsonl(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    recorder.set_context_providers(epoch=lambda: 7, state=lambda: "LISTENING")
    recorder.record("realtime.connected", model="gpt-realtime-2.1")
    recorder.close()

    (entry,) = read_lines(tmp_path / "events.jsonl")
    assert entry["event"] == "realtime.connected"
    assert entry["connection_epoch"] == 7
    assert entry["session_state"] == "LISTENING"
    assert entry["model"] == "gpt-realtime-2.1"
    assert entry["timestamp"].endswith("+00:00")


def test_record_without_providers_omits_context_fields(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    recorder.record("app.start")
    recorder.close()
    (entry,) = read_lines(tmp_path / "events.jsonl")
    assert "connection_epoch" not in entry
    assert "session_state" not in entry


def test_record_redacts_nested_field_values(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    recorder.record(
        "realtime.error",
        message="auth failed for sk-test-proj-abcdef1234567890",
        detail={"headers": ["Bearer sk-test-proj-abcdef1234567890"]},
    )
    recorder.close()
    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "sk-test-proj-abcdef1234567890" not in raw
    assert "sk-***" in raw


def test_rotation_keeps_bounded_files(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path, max_bytes=500, keep_files=2)
    for index in range(60):
        recorder.record("fsm.transition", index=index, padding="x" * 40)
    recorder.close()

    assert path.exists()
    assert (tmp_path / "events.jsonl.1").exists()
    assert not (tmp_path / "events.jsonl.3").exists()
    # newest file stays small after rotation
    assert path.stat().st_size < 5_000


def test_record_survives_real_write_failure(monkeypatch, tmp_path) -> None:
    # Patch the file's write() so every call raises — this exercises the
    # broad Exception handler, not just the happy mkdir path.
    recorder = EventRecorder(tmp_path / "events.jsonl")

    # Trigger file creation by writing one good record first
    recorder.record("app.start")

    def _always_raise(data):
        raise OSError("disk full")

    monkeypatch.setattr(recorder._file, "write", _always_raise)
    recorder.record("should_not_raise")  # must not propagate
    recorder.close()


def test_record_is_thread_safe(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path)
    threads = [
        threading.Thread(target=lambda: [recorder.record("tick") for _ in range(50)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    recorder.close()
    assert len(read_lines(path)) == 200


def test_record_redacts_objects_stringified_by_json_default(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")
    secret = "sk-test-abcdefghijklmnopqrstuvwxyz"
    recorder.record("test.event", error=RuntimeError(f"auth failed for {secret}"))

    written = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert secret not in written
    assert redact_secrets(secret) in written


def test_provider_exception_does_not_break_recording(tmp_path) -> None:
    recorder = EventRecorder(tmp_path / "events.jsonl")

    def broken_epoch() -> int:
        raise RuntimeError("not connected yet")

    recorder.set_context_providers(epoch=broken_epoch, state=lambda: "DISCONNECTED")
    recorder.record("app.start")
    recorder.close()
    (entry,) = read_lines(tmp_path / "events.jsonl")
    assert "connection_epoch" not in entry
    assert entry["session_state"] == "DISCONNECTED"
