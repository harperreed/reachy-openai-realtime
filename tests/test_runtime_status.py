from reachy_openai_realtime.runtime_status import RuntimeStatus, safe_message


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
