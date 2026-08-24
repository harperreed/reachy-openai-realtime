# ABOUTME: Tests the fixed rolling turn-rate safety policy.
# ABOUTME: Boundary cases prove turns expire by monotonic timestamp alone.

from reachy_openai_realtime.session.circuit_breaker import (
    SESSION_LIMIT_SECONDS,
    TURN_RATE_LIMIT,
    TURN_RATE_WINDOW_SECONDS,
    TurnRateCircuitBreaker,
)


def test_fixed_safety_limits() -> None:
    assert TURN_RATE_LIMIT == 5
    assert TURN_RATE_WINDOW_SECONDS == 60.0
    assert SESSION_LIMIT_SECONDS == 30 * 60.0


def test_trips_on_fifth_turn_inside_window() -> None:
    breaker = TurnRateCircuitBreaker()
    assert [breaker.record_turn(float(second)) for second in (0, 10, 20, 30)] == [False] * 4
    assert breaker.record_turn(59.999) is True
    assert breaker.turn_count == 5


def test_turn_at_sixty_second_boundary_expires() -> None:
    breaker = TurnRateCircuitBreaker()
    for second in (0, 10, 20, 30):
        assert breaker.record_turn(float(second)) is False
    assert breaker.record_turn(60.0) is False
    assert breaker.turn_count == 4


def test_old_turns_expire_without_transcript_input() -> None:
    breaker = TurnRateCircuitBreaker()
    for second in (1, 2, 3, 4):
        assert breaker.record_turn(float(second)) is False
    assert breaker.record_turn(120.0) is False
    assert breaker.turn_count == 1
