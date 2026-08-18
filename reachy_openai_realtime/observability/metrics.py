# ABOUTME: In-process latency/counter/gauge metrics with bounded windows,
# ABOUTME: exposed through RuntimeStatus.snapshot() into /api/diagnostics.
from __future__ import annotations

import threading
from collections import deque
from typing import Any


def _percentile(data: list[float], percent: float) -> float:
    index = round((len(data) - 1) * percent / 100.0)
    return data[index]


class LatencyStat:
    """Rolling latency aggregate: lifetime count, window-bounded percentiles."""

    def __init__(self, window: int = 200) -> None:
        self._values: deque[float] = deque(maxlen=window)
        self._count = 0

    def record(self, value_ms: float) -> None:
        self._count += 1
        self._values.append(float(value_ms))

    def snapshot(self) -> dict[str, float | int]:
        if not self._values:
            return {"count": self._count, "min": 0.0, "max": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0}
        data = sorted(self._values)
        return {
            "count": self._count,
            "min": data[0],
            "max": data[-1],
            "mean": sum(data) / len(data),
            "p50": _percentile(data, 50.0),
            "p95": _percentile(data, 95.0),
        }


class MetricsRegistry:
    """Thread-safe named metrics: latency observations, counters, gauges."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latency: dict[str, LatencyStat] = {}
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}

    def observe_ms(self, name: str, value_ms: float) -> None:
        with self._lock:
            stat = self._latency.get(name)
            if stat is None:
                stat = self._latency[name] = LatencyStat()
            stat.record(value_ms)

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "latency": {name: stat.snapshot() for name, stat in self._latency.items()},
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }
