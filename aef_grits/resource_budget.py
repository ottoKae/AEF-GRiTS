"""Memory-aware resource planning for bounded AEF download pipelines."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import time
from typing import Any


MIB = 1024**2
GIB = 1024**3
DEFAULT_GLOBAL_REQUESTS = 8
GRID_WORKER_CAP = 4
POINT_WORKER_CAP = 4

RESOURCE_PROFILES = {
    "workstation-auto": {
        "memory_limit_gib": "auto",
        "memory_reserve_gib": "auto",
        "global_request_limit": 8,
    },
    "low-memory-1g": {
        "memory_limit_gib": 1.0,
        "memory_reserve_gib": 0.5,
        "global_request_limit": 2,
    },
    "server-8g": {
        "memory_limit_gib": 8.0,
        "memory_reserve_gib": 2.0,
        "global_request_limit": 4,
    },
    "server-16g": {
        "memory_limit_gib": 16.0,
        "memory_reserve_gib": 2.0,
        "global_request_limit": 8,
    },
}


def resolve_resource_profile(
    name: str,
    *,
    memory_limit_gib: float | str | None = None,
    memory_reserve_gib: float | str | None = None,
    global_request_limit: int | None = None,
) -> dict[str, Any]:
    """Resolve a named conservative profile with explicit CLI overrides."""
    if name not in RESOURCE_PROFILES:
        raise ValueError(
            f"Unknown resource profile {name!r}; expected {sorted(RESOURCE_PROFILES)}"
        )
    profile = dict(RESOURCE_PROFILES[name])
    if memory_limit_gib is not None:
        profile["memory_limit_gib"] = memory_limit_gib
    if memory_reserve_gib is not None:
        profile["memory_reserve_gib"] = memory_reserve_gib
    if global_request_limit is not None:
        profile["global_request_limit"] = int(global_request_limit)
    profile["name"] = name
    return profile


def _read_int(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value == "max":
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None
    except (OSError, ValueError):
        return None


def _host_memory() -> tuple[int, int]:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return int(memory.total), int(memory.available)
    except (ImportError, OSError):
        pages = int(os.sysconf("SC_PHYS_PAGES")) if hasattr(os, "sysconf") else 0
        page_size = int(os.sysconf("SC_PAGE_SIZE")) if pages else 0
        total = pages * page_size if pages and page_size else 8 * GIB
        return total, total


def _cgroup_headroom(root: str | Path = "/sys/fs/cgroup") -> tuple[int | None, int | None]:
    if os.name != "posix" and os.fspath(root) == "/sys/fs/cgroup":
        return None, None
    base = Path(root)
    limit = _read_int(base / "memory.max")
    current = _read_int(base / "memory.current")
    if limit is not None:
        return limit, max(0, limit - (current or 0))
    # cgroup v1 fallback.
    v1 = base / "memory"
    limit = _read_int(v1 / "memory.limit_in_bytes")
    current = _read_int(v1 / "memory.usage_in_bytes")
    if limit is not None and limit < 1 << 60:
        return limit, max(0, limit - (current or 0))
    return None, None


@dataclass(frozen=True)
class MemorySnapshot:
    host_total_bytes: int
    host_available_bytes: int
    cgroup_limit_bytes: int | None
    cgroup_available_bytes: int | None
    explicit_limit_bytes: int | None
    effective_available_bytes: int
    reserve_bytes: int
    usable_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def memory_snapshot(
    *,
    limit_gib: float | str | None = "auto",
    reserve_gib: float | str | None = "auto",
    cgroup_root: str | Path = "/sys/fs/cgroup",
) -> MemorySnapshot:
    host_total, host_available = _host_memory()
    cgroup_limit, cgroup_available = _cgroup_headroom(cgroup_root)
    explicit = None
    if limit_gib not in (None, "auto"):
        explicit = max(1, int(float(limit_gib) * GIB))
    candidates = [host_available]
    if cgroup_available is not None:
        candidates.append(cgroup_available)
    if explicit is not None:
        candidates.append(explicit)
    effective = max(0, min(candidates))
    if reserve_gib in (None, "auto"):
        reserve = max(512 * MIB, min(2 * GIB, int(effective * 0.15)))
    else:
        reserve = max(0, int(float(reserve_gib) * GIB))
    usable = max(64 * MIB, effective - reserve)
    return MemorySnapshot(
        host_total_bytes=host_total,
        host_available_bytes=host_available,
        cgroup_limit_bytes=cgroup_limit,
        cgroup_available_bytes=cgroup_available,
        explicit_limit_bytes=explicit,
        effective_available_bytes=effective,
        reserve_bytes=reserve,
        usable_bytes=usable,
    )


@dataclass(frozen=True)
class ResourcePlan:
    workflow: str
    requested_workers: str
    workers: int
    max_in_flight: int
    request_bytes: int
    estimated_peak_bytes: int
    usable_memory_bytes: int
    effective_available_bytes: int
    reserve_bytes: int
    global_request_limit: int
    high_watermark: float
    critical_watermark: float

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["estimated_peak_gib"] = self.estimated_peak_bytes / GIB
        payload["usable_memory_gib"] = self.usable_memory_bytes / GIB
        return payload


def _parse_workers(value: int | str, cap: int) -> tuple[str, int | None]:
    token = str(value).strip().lower()
    if token == "auto":
        return token, None
    try:
        parsed = int(token)
    except ValueError as exc:
        raise ValueError("workers must be a positive integer or 'auto'") from exc
    if parsed <= 0:
        raise ValueError("workers must be a positive integer or 'auto'")
    return token, min(parsed, cap)


def plan_grid_resources(
    *,
    block_size: int,
    workers: int | str,
    snapshot: MemorySnapshot,
    global_request_limit: int = DEFAULT_GLOBAL_REQUESTS,
    high_watermark: float = 0.80,
    critical_watermark: float = 0.90,
) -> ResourcePlan:
    if not 0 < high_watermark < critical_watermark < 1:
        raise ValueError("memory watermarks must satisfy 0 < high < critical < 1")
    raw = int(block_size) ** 2 * 64 * 4
    # Baseline Python/Zarr state plus one uncompressed shard and compression
    # scratch. Each request may temporarily hold the EE response and stacked
    # float32 array; each in-flight completion retains one stacked array.
    base_writer = 256 * MIB
    per_worker = max(32 * MIB, int(raw * 2.5))
    per_result = raw
    requested, explicit = _parse_workers(workers, GRID_WORKER_CAP)
    cpu_cap = min(GRID_WORKER_CAP, max(1, os.cpu_count() or 1))
    request_cap = max(1, min(int(global_request_limit), cpu_cap))

    def peak(count: int, in_flight: int) -> int:
        return base_writer + count * per_worker + in_flight * per_result

    if explicit is None:
        resolved = 1
        for candidate in range(1, request_cap + 1):
            if peak(candidate, candidate) <= snapshot.usable_bytes * high_watermark:
                resolved = candidate
    else:
        resolved = min(explicit, request_cap)
    max_in_flight = resolved
    estimate = peak(resolved, max_in_flight)
    if estimate > snapshot.usable_bytes * critical_watermark:
        raise ValueError(
            f"Grid concurrency needs about {estimate / GIB:.2f} GiB but the "
            f"critical memory budget is {snapshot.usable_bytes * critical_watermark / GIB:.2f} GiB"
        )
    return ResourcePlan(
        workflow="grid",
        requested_workers=requested,
        workers=resolved,
        max_in_flight=max_in_flight,
        request_bytes=raw,
        estimated_peak_bytes=estimate,
        usable_memory_bytes=snapshot.usable_bytes,
        effective_available_bytes=snapshot.effective_available_bytes,
        reserve_bytes=snapshot.reserve_bytes,
        global_request_limit=max(1, int(global_request_limit)),
        high_watermark=high_watermark,
        critical_watermark=critical_watermark,
    )


def plan_point_resources(
    *,
    chunk_size: int,
    years: int,
    workers: int | str,
    snapshot: MemorySnapshot,
    global_request_limit: int = DEFAULT_GLOBAL_REQUESTS,
    high_watermark: float = 0.80,
    critical_watermark: float = 0.90,
) -> ResourcePlan:
    raw = int(chunk_size) * max(1, int(years)) * 64 * 8
    base = 256 * MIB
    per_worker = max(24 * MIB, raw * 4)
    requested, explicit = _parse_workers(workers, POINT_WORKER_CAP)
    request_cap = max(1, min(int(global_request_limit), POINT_WORKER_CAP, os.cpu_count() or 1))

    def peak(count: int) -> int:
        return base + count * per_worker

    if explicit is None:
        resolved = 1
        for candidate in range(1, request_cap + 1):
            if peak(candidate) <= snapshot.usable_bytes * high_watermark:
                resolved = candidate
    else:
        resolved = min(explicit, request_cap)
    estimate = peak(resolved)
    if estimate > snapshot.usable_bytes * critical_watermark:
        raise ValueError(
            f"Point concurrency needs about {estimate / GIB:.2f} GiB but the "
            f"critical memory budget is {snapshot.usable_bytes * critical_watermark / GIB:.2f} GiB"
        )
    return ResourcePlan(
        workflow="points",
        requested_workers=requested,
        workers=resolved,
        max_in_flight=resolved,
        request_bytes=raw,
        estimated_peak_bytes=estimate,
        usable_memory_bytes=snapshot.usable_bytes,
        effective_available_bytes=snapshot.effective_available_bytes,
        reserve_bytes=snapshot.reserve_bytes,
        global_request_limit=max(1, int(global_request_limit)),
        high_watermark=high_watermark,
        critical_watermark=critical_watermark,
    )


class ResourceLimitError(RuntimeError):
    """Raised after a bounded pipeline reaches its critical memory limit."""


class MemoryGuard:
    def __init__(self, plan: ResourcePlan):
        self.plan = plan
        self.peak_rss_bytes = 0
        self.throttle_count = 0
        self._last_system_check = 0.0
        self._last_system_available = plan.effective_available_bytes

    def rss_bytes(self) -> int:
        try:
            import psutil

            process = psutil.Process(os.getpid())
            total = process.memory_info().rss
            for child in process.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except psutil.Error:
                    pass
            return int(total)
        except (ImportError, OSError):
            return 0

    def sample(self) -> str:
        rss = self.rss_bytes()
        self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
        now = time.monotonic()
        if now - self._last_system_check >= 0.5:
            self._last_system_check = now
            self._last_system_available = memory_snapshot(reserve_gib=0).effective_available_bytes
        if self._last_system_available <= self.plan.reserve_bytes:
            return "critical"
        if self._last_system_available <= (
            self.plan.reserve_bytes + int(self.plan.effective_available_bytes * 0.10)
        ):
            self.throttle_count += 1
            return "high"
        if rss >= self.plan.usable_memory_bytes * self.plan.critical_watermark:
            return "critical"
        if rss >= self.plan.usable_memory_bytes * self.plan.high_watermark:
            self.throttle_count += 1
            return "high"
        return "normal"

    def wait_below_high(self, timeout_seconds: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while self.sample() == "high" and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.sample() == "critical":
            raise ResourceLimitError("Process RSS reached the critical memory watermark")

    def report(self) -> dict[str, Any]:
        return {
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_rss_mb": self.peak_rss_bytes / MIB,
            "memory_throttle_count": self.throttle_count,
        }
