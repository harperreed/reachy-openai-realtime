# ABOUTME: Unit tests for BackoffPolicy, ErrorClass, and classify_connection_error.
# ABOUTME: TDD first-pass — all tests written before the module exists.
import random

from reachy_openai_realtime.session.recovery import (
    BackoffPolicy,
    ErrorClass,
    classify_connection_error,
)


def test_backoff_sequence_caps_at_thirty_seconds() -> None:
    policy = BackoffPolicy(jitter_ratio=0.0)
    delays = [policy.next_delay() for _ in range(8)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 30.0, 30.0]


def test_jitter_stays_within_twenty_percent() -> None:
    policy = BackoffPolicy(rng=random.Random(7))
    for expected in BackoffPolicy.DELAYS:
        delay = policy.next_delay()
        assert expected * 0.8 <= delay <= expected * 1.2


def test_healthy_session_resets_backoff() -> None:
    policy = BackoffPolicy(jitter_ratio=0.0)
    for _ in range(4):
        policy.next_delay()
    policy.note_session_duration(61.0)
    assert policy.next_delay() == 1.0


def test_short_session_does_not_reset_backoff() -> None:
    policy = BackoffPolicy(jitter_ratio=0.0)
    policy.next_delay()
    policy.note_session_duration(5.0)
    assert policy.next_delay() == 2.0


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class AuthenticationError(Exception):
    pass


def test_classification_table() -> None:
    assert classify_connection_error(_StatusError(401)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(403)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(404)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(422)) is ErrorClass.FATAL_CONFIG
    assert classify_connection_error(_StatusError(429)) is ErrorClass.TRANSIENT
    assert classify_connection_error(_StatusError(500)) is ErrorClass.TRANSIENT
    assert classify_connection_error(_StatusError(503)) is ErrorClass.TRANSIENT
    assert classify_connection_error(ConnectionError("reset")) is ErrorClass.TRANSIENT
    assert classify_connection_error(OSError("network down")) is ErrorClass.TRANSIENT
    # SDK exception types are matched by name when no status code is exposed
    assert classify_connection_error(AuthenticationError("bad key")) is ErrorClass.FATAL_CONFIG


def test_nested_response_status_is_found() -> None:
    class Handshake(Exception):
        def __init__(self) -> None:
            super().__init__("rejected")
            self.response = type("Resp", (), {"status_code": 401})()

    assert classify_connection_error(Handshake()) is ErrorClass.FATAL_CONFIG
