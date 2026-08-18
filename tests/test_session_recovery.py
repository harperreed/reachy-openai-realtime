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


def test_session_cap_close_is_transient() -> None:
    # The 60-min Realtime session cap sends a connection close with no HTTP
    # status code — the SDK surfaces this as APIConnectionError (or a plain
    # network exception). Must classify TRANSIENT so the session reconnects.
    # Note: make_session's AsyncOpenAI() ctor depends on the env key fixture;
    # fast_sleep sets stop before awaiting so the loop condition, not the
    # sleep, observes the stop. Neither applies here — pure error-classification test.
    class APIConnectionError(Exception):
        """Mimics openai.APIConnectionError: no status_code attribute."""

    exc = APIConnectionError("server closed connection after 60 min session cap")
    # No status code must be reachable via the hierarchy
    assert not hasattr(exc, "status_code")
    assert classify_connection_error(exc) is ErrorClass.TRANSIENT
