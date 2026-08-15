"""Thread-safe operational telemetry for direct AEF downloads."""

from __future__ import annotations

from contextlib import contextmanager
import math
import threading
import time
from typing import Any


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


class RunTelemetry:
    """Collect bounded request, token, transfer and write measurements."""

    def __init__(self, max_latency_samples: int = 100_000):
        self._lock = threading.Lock()
        self._latencies: list[float] = []
        self._max_latency_samples = max(1, int(max_latency_samples))
        self.requests_started = 0
        self.requests_succeeded = 0
        self.requests_failed = 0
        self.request_retries = 0
        self.request_timeouts = 0
        self.bytes_received = 0
        self.token_wait_seconds = 0.0
        self.write_seconds = 0.0
        self.delivery_seconds = 0.0

    def request_event(self, name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            if name == "request_started":
                self.requests_started += 1
            elif name == "request_succeeded":
                self.requests_succeeded += 1
            elif name == "request_retry":
                self.request_retries += 1
            elif name == "request_timeout":
                self.request_timeouts += 1
                self.requests_failed += 1
            elif name == "request_failed":
                self.requests_failed += 1
            elapsed = payload.get("elapsed_seconds")
            if elapsed is not None and name in {
                "request_succeeded",
                "request_retry",
                "request_timeout",
                "request_failed",
            }:
                if len(self._latencies) < self._max_latency_samples:
                    self._latencies.append(float(elapsed))

    @contextmanager
    def request_token(self, pool):
        started = time.perf_counter()
        with pool.token():
            waited = time.perf_counter() - started
            with self._lock:
                self.token_wait_seconds += waited
            yield

    @contextmanager
    def measure_write(self):
        started = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self.write_seconds += time.perf_counter() - started

    @contextmanager
    def measure_delivery(self):
        started = time.perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self.delivery_seconds += time.perf_counter() - started

    def add_received_bytes(self, count: int) -> None:
        with self._lock:
            self.bytes_received += max(0, int(count))

    def report(self) -> dict[str, Any]:
        with self._lock:
            latencies = list(self._latencies)
            return {
                "requests_started": self.requests_started,
                "requests_succeeded": self.requests_succeeded,
                "requests_failed": self.requests_failed,
                "request_retries": self.request_retries,
                "request_timeouts": self.request_timeouts,
                "request_latency_seconds_p50": _percentile(latencies, 0.50),
                "request_latency_seconds_p95": _percentile(latencies, 0.95),
                "request_latency_seconds_max": max(latencies) if latencies else None,
                "earth_engine_response_bytes_estimated": self.bytes_received,
                "earth_engine_response_mib_estimated": self.bytes_received / 1024**2,
                "request_token_wait_seconds": self.token_wait_seconds,
                "zarr_or_parquet_write_seconds": self.write_seconds,
                "final_delivery_seconds": self.delivery_seconds,
            }
