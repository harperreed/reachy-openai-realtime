import threading

from reachy_openai_realtime.observability.metrics import LatencyStat, MetricsRegistry


def test_latency_stat_aggregates() -> None:
    stat = LatencyStat()
    for value in [100.0, 200.0, 300.0, 400.0]:
        stat.record(value)
    snapshot = stat.snapshot()
    assert snapshot["count"] == 4
    assert snapshot["min"] == 100.0
    assert snapshot["max"] == 400.0
    assert snapshot["mean"] == 250.0
    assert 200.0 <= snapshot["p50"] <= 300.0
    assert snapshot["p95"] == 400.0


def test_latency_stat_empty_snapshot_is_zeroed() -> None:
    snapshot = LatencyStat().snapshot()
    assert snapshot == {"count": 0, "min": 0.0, "max": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0}


def test_latency_stat_window_bounds_memory_but_count_is_lifetime() -> None:
    stat = LatencyStat(window=10)
    for value in range(100):
        stat.record(float(value))
    snapshot = stat.snapshot()
    assert snapshot["count"] == 100
    assert snapshot["min"] == 90.0  # only the recent window remains


def test_registry_snapshot_shape() -> None:
    registry = MetricsRegistry()
    registry.observe_ms("speech_end_to_first_audio_played_ms", 640.0)
    registry.increment("reconnect_count")
    registry.increment("reconnect_count", 2)
    registry.set_gauge("queued_audio_ms", 180.0)
    snapshot = registry.snapshot()
    assert snapshot["latency"]["speech_end_to_first_audio_played_ms"]["count"] == 1
    assert snapshot["counters"]["reconnect_count"] == 3
    assert snapshot["gauges"]["queued_audio_ms"] == 180.0


def test_registry_is_thread_safe() -> None:
    registry = MetricsRegistry()
    threads = [
        threading.Thread(target=lambda: [registry.increment("ticks") for _ in range(1000)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert registry.snapshot()["counters"]["ticks"] == 4000


def test_percentile_tie_case_avoids_banker_rounding() -> None:
    # With banker's rounding, round(0.5) == 0 (rounds to even), so
    # _percentile([10.0, 20.0], 50.0) would return 10.0 instead of 20.0.
    # Nearest-rank must return the upper value on an exact tie.
    stat = LatencyStat()
    stat.record(10.0)
    stat.record(20.0)
    snap = stat.snapshot()
    # With 2 values, p50 index = int((2-1)*50/100 + 0.5) = int(1.0) = 1 → 20.0
    assert snap["p50"] == 20.0
