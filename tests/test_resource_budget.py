from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time

import pytest

import aef_grits.resource_budget as budget
from aef_grits.resource_budget import (
    GIB,
    MemorySnapshot,
    memory_snapshot,
    plan_grid_resources,
    plan_point_resources,
    resolve_resource_profile,
)
from aef_grits.telemetry import RunTelemetry
from aef_grits.resource_control import FileLease, TokenPool


def _snapshot(gib: float) -> MemorySnapshot:
    value = int(gib * GIB)
    return MemorySnapshot(value, value, None, None, value, value, 0, value)


def test_cgroup_limit_wins_over_host_memory(tmp_path, monkeypatch):
    (tmp_path / "memory.max").write_text(str(4 * GIB), encoding="utf-8")
    (tmp_path / "memory.current").write_text(str(1 * GIB), encoding="utf-8")
    monkeypatch.setattr(budget, "_host_memory", lambda: (64 * GIB, 40 * GIB))
    snapshot = memory_snapshot(cgroup_root=tmp_path, reserve_gib=0)
    assert snapshot.effective_available_bytes == 3 * GIB
    assert snapshot.usable_bytes == 3 * GIB


def test_grid_auto_workers_are_memory_bounded():
    low = plan_grid_resources(block_size=433, workers="auto", snapshot=_snapshot(0.55))
    high = plan_grid_resources(block_size=256, workers="auto", snapshot=_snapshot(16))
    assert low.workers == 1
    assert low.max_in_flight == 1
    assert 1 <= high.workers <= 4
    assert high.estimated_peak_bytes < high.usable_memory_bytes * high.critical_watermark


def test_explicit_unsafe_grid_concurrency_is_rejected():
    with pytest.raises(ValueError, match="critical memory budget"):
        plan_grid_resources(block_size=433, workers=8, snapshot=_snapshot(0.45))


def test_point_year_count_changes_estimated_request_memory():
    one = plan_point_resources(
        chunk_size=10000, years=1, workers=2, snapshot=_snapshot(4)
    )
    nine = plan_point_resources(
        chunk_size=10000, years=9, workers=2, snapshot=_snapshot(4)
    )
    assert nine.request_bytes == one.request_bytes * 9
    assert nine.estimated_peak_bytes > one.estimated_peak_bytes


def test_named_resource_profile_preserves_explicit_overrides():
    profile = resolve_resource_profile(
        "low-memory-1g",
        global_request_limit=1,
    )
    assert profile["memory_limit_gib"] == 1.0
    assert profile["memory_reserve_gib"] == 0.5
    assert profile["global_request_limit"] == 1


def test_run_telemetry_reports_request_and_io_measurements():
    telemetry = RunTelemetry()
    telemetry.request_event("request_started", {})
    telemetry.request_event("request_retry", {"elapsed_seconds": 2.0})
    telemetry.request_event("request_started", {})
    telemetry.request_event("request_succeeded", {"elapsed_seconds": 1.0})
    telemetry.add_received_bytes(1024**2)
    with telemetry.measure_write():
        pass
    report = telemetry.report()
    assert report["requests_started"] == 2
    assert report["requests_succeeded"] == 1
    assert report["request_retries"] == 1
    assert report["request_latency_seconds_p50"] == 1.5
    assert report["earth_engine_response_mib_estimated"] == 1.0


def test_token_pool_enforces_global_concurrency(tmp_path):
    pool = TokenPool(tmp_path / "tokens", 2)
    active = 0
    peak = 0

    def work():
        nonlocal active, peak
        with pool.token(timeout_seconds=5):
            active += 1
            peak = max(peak, active)
            time.sleep(0.05)
            active -= 1

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda _: work(), range(6)))
    assert peak == 2


def test_exclusive_file_lease_times_out_while_held(tmp_path):
    first = FileLease(tmp_path / "delivery.lock")
    first.acquire(timeout_seconds=1)
    try:
        second = FileLease(tmp_path / "delivery.lock")
        with pytest.raises(TimeoutError):
            second.acquire(timeout_seconds=0.1, poll_seconds=0.01)
    finally:
        first.release()
