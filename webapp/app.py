"""Local AEF-GRiTS web application and reliable task runner."""

from __future__ import annotations

import json
import hashlib
import atexit
import math
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
import psutil
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.earth_engine import AEF_FIRST_YEAR, AEF_LAST_YEAR, DATASET
from aef_grits.atomic import write_json, write_text
from aef_grits.resources import BUILTIN_MGRS_INDEX
from aef_grits.grid_lookup import resolve_mgrs, resolve_tessera
from aef_grits.grids import MGRSGridProvider, Tessera01GridProvider
from aef_grits.point_source import PointSource, preflight_point_source
from aef_grits.resource_budget import (
    DEFAULT_GLOBAL_REQUESTS,
    memory_snapshot,
    plan_grid_resources,
    plan_point_resources,
)
from aef_grits.redaction import redact_text
from aef_grits.storage_safety import (
    isolated_disk_free,
    mount_for_path,
    terminate_process_group_once,
    validate_storage_layout,
)
from webapp.auth import AuthBinding, WebAuth


APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = Path(
    os.environ.get("AEF_GRITS_WEB_STATE", str(APP_DIR / "runs"))
).expanduser().absolute()
OUTPUT_ROOT = Path(
    os.environ.get("AEF_GRITS_WEB_OUTPUT", str(APP_DIR / "output"))
).expanduser().absolute()
STAGING_ROOT = (
    Path(os.environ["AEF_GRITS_WEB_STAGING"]).expanduser().absolute()
    if os.environ.get("AEF_GRITS_WEB_STAGING")
    else None
)
STATIC_DIR = APP_DIR / "static"
SERVER_PID_PATH = RUNS_DIR / "server.pid"
MAX_CONCURRENT_TASKS = max(1, int(os.environ.get("AEF_GRITS_WEB_CONCURRENCY", "2")))
MAX_QUEUED_TASKS = max(0, int(os.environ.get("AEF_GRITS_WEB_MAX_QUEUE", "20")))
MAX_BYPASS_SECONDS = max(
    0.0, float(os.environ.get("AEF_GRITS_WEB_MAX_BYPASS_SECONDS", "30"))
)
MAX_GRID_COUNT = max(1, int(os.environ.get("AEF_GRITS_WEB_MAX_GRIDS", "500")))
MAX_UPLOAD_BYTES = int(os.environ.get("AEF_GRITS_WEB_MAX_UPLOAD_MB", "512")) * 1024**2
MAX_RAW_BYTES = int(float(os.environ.get("AEF_GRITS_WEB_MAX_RAW_GIB", "100")) * 1024**3)
WARN_RAW_BYTES = int(float(os.environ.get("AEF_GRITS_WEB_WARN_RAW_GIB", "10")) * 1024**3)
DISK_FACTOR = max(0.0, float(os.environ.get("AEF_GRITS_WEB_DISK_FACTOR", "1.0")))
DISK_RESERVE_BYTES = int(float(os.environ.get("AEF_GRITS_WEB_DISK_RESERVE_GIB", "2")) * 1024**3)
PLAN_TTL_SECONDS = max(60, int(os.environ.get("AEF_GRITS_WEB_PLAN_TTL", "3600")))
VALIDATION_TIMEOUT_SECONDS = max(
    10, int(os.environ.get("AEF_GRITS_WEB_VALIDATION_TIMEOUT", "300"))
)
GLOBAL_REQUEST_LIMIT = max(
    1, int(os.environ.get("AEF_GRITS_WEB_GLOBAL_REQUESTS", str(DEFAULT_GLOBAL_REQUESTS)))
)
RESOURCE_PROFILE = os.environ.get(
    "AEF_GRITS_WEB_RESOURCE_PROFILE", "workstation-auto"
).strip()
if RESOURCE_PROFILE not in {"workstation-auto", "low-memory-1g", "server-8g", "server-16g"}:
    raise ValueError("AEF_GRITS_WEB_RESOURCE_PROFILE is invalid")
WEB_MEMORY_SNAPSHOT = memory_snapshot(
    limit_gib=os.environ.get("AEF_GRITS_WEB_MEMORY_GIB", "auto"),
    reserve_gib=os.environ.get("AEF_GRITS_WEB_MEMORY_RESERVE_GIB", "auto"),
)
RESOURCE_STATE_ROOT = Path(
    os.environ.get("AEF_GRITS_RESOURCE_STATE", str(RUNS_DIR / ".resource_locks"))
).expanduser().absolute()
POINT_SUFFIXES = {".csv", ".parquet", ".shp", ".gpkg", ".geojson", ".json"}
TERMINAL_STATUSES = {
    "done", "failed", "error", "cancelled", "interrupted", "auth_required"
}
PROGRESS_PATTERN = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")
GRID_ID_PATTERN = re.compile(r'"grid_id"\s*:\s*"([^"]+)"')
EVENT_PREFIX = "AEF_EVENT "

STORAGE_LAYOUT = validate_storage_layout(OUTPUT_ROOT, RUNS_DIR, STAGING_ROOT)
_resource_mount = mount_for_path(RESOURCE_STATE_ROOT)
if _resource_mount and _resource_mount.is_linux_ntfs:
    raise ValueError("AEF_GRITS_RESOURCE_STATE must be on a native filesystem")
if STORAGE_LAYOUT.final_mount and STORAGE_LAYOUT.final_mount.is_linux_ntfs and _resource_mount is None:
    raise ValueError("Cannot verify AEF_GRITS_RESOURCE_STATE for NTFS output")
RESOURCE_STATE_ROOT.mkdir(parents=True, exist_ok=True)
if not (STORAGE_LAYOUT.final_mount and STORAGE_LAYOUT.final_mount.is_linux_ntfs):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
PLANS_DIR = RUNS_DIR / "plans"
PLANS_DIR.mkdir(parents=True, exist_ok=True)


def _detect_python() -> str:
    """Use the current interpreter when possible, otherwise one with Earth Engine."""
    candidates = [sys.executable]
    candidates.extend(filter(None, (shutil.which("python"), shutil.which("python3"))))
    for candidate in dict.fromkeys(candidates):
        try:
            result = subprocess.run(
                [candidate, "-c", "import ee"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    return sys.executable


PYTHON_BIN = _detect_python()


def _plan_secret() -> str:
    configured = os.environ.get("AEF_GRITS_WEB_PLAN_SECRET")
    if configured:
        return configured
    path = RUNS_DIR / ".plan_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    write_text(value, path)
    return value


_plan_serializer = URLSafeTimedSerializer(_plan_secret(), salt="aef-grits-web-plan-v1")


def _session_secret() -> str:
    configured = os.environ.get("AEF_GRITS_WEB_SESSION_SECRET")
    if configured:
        return configured
    path = RUNS_DIR / ".session_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    write_text(value, path)
    return value


_web_auth = WebAuth()
_trusted_hosts = [
    value.strip()
    for value in os.environ.get(
        "AEF_GRITS_WEB_TRUSTED_HOSTS", "localhost,127.0.0.1"
    ).split(",")
    if value.strip()
]

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.secret_key = _session_secret()
app.config.update(
    JSON_AS_ASCII=False,
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    TRUSTED_HOSTS=_trusted_hosts,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(_web_auth.mode == "oauth" and not _web_auth.allow_insecure),
)
_proxy_count = max(0, int(os.environ.get("AEF_GRITS_WEB_PROXY_COUNT", "0")))
if _proxy_count:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=_proxy_count,
        x_proto=_proxy_count,
        x_host=_proxy_count,
        x_port=_proxy_count,
        x_prefix=_proxy_count,
    )

_tasks: dict[str, dict[str, Any]] = {}
_processes: dict[str, subprocess.Popen[str]] = {}
_task_lock = threading.RLock()
_executor = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_TASKS + MAX_QUEUED_TASKS,
    thread_name_prefix="aef-web-worker",
)
_submission_slots = threading.BoundedSemaphore(MAX_CONCURRENT_TASKS + MAX_QUEUED_TASKS)


class _MemoryAdmission:
    """Combined memory/process admission with bounded, aging backfill."""

    def __init__(
        self,
        capacity_bytes: int,
        max_active: int = 1_000_000,
        max_bypass_seconds: float = 30.0,
    ):
        self.capacity_bytes = int(capacity_bytes)
        self.max_active = max(1, int(max_active))
        self.max_bypass_seconds = max(0.0, float(max_bypass_seconds))
        self.used_bytes = 0
        self.active = 0
        self.waiters: list[dict[str, Any]] = []
        self.condition = threading.Condition()

    def acquire(
        self,
        amount: int,
        cancel_event: threading.Event,
        request_id: str | None = None,
    ) -> bool:
        amount = int(amount)
        if amount <= 0 or amount > self.capacity_bytes:
            return False
        waiter = {
            "id": request_id or uuid.uuid4().hex,
            "amount": amount,
            "queued_at": time.monotonic(),
        }
        with self.condition:
            self.waiters.append(waiter)
            while True:
                if cancel_event.is_set():
                    if waiter in self.waiters:
                        self.waiters.remove(waiter)
                    self.condition.notify_all()
                    return False
                first = self.waiters[0]
                fits = (
                    self.active < self.max_active
                    and self.used_bytes + amount <= self.capacity_bytes
                )
                first_fits = (
                    self.active < self.max_active
                    and self.used_bytes + int(first["amount"]) <= self.capacity_bytes
                )
                oldest_age = time.monotonic() - float(first["queued_at"])
                eligible = waiter is first or (
                    not first_fits and oldest_age < self.max_bypass_seconds
                )
                if fits and eligible:
                    self.waiters.remove(waiter)
                    self.used_bytes += amount
                    self.active += 1
                    self.condition.notify_all()
                    return True
                self.condition.wait(timeout=0.25)

    def release(self, amount: int) -> None:
        with self.condition:
            self.used_bytes = max(0, self.used_bytes - int(amount))
            self.active = max(0, self.active - 1)
            self.condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return {
                "capacity_bytes": self.capacity_bytes,
                "used_bytes": self.used_bytes,
                "available_bytes": max(0, self.capacity_bytes - self.used_bytes),
                "active_tasks": self.active,
                "max_active_tasks": self.max_active,
                "waiting_tasks": len(self.waiters),
            }


_memory_admission = _MemoryAdmission(
    WEB_MEMORY_SNAPSHOT.usable_bytes,
    max_active=MAX_CONCURRENT_TASKS,
    max_bypass_seconds=MAX_BYPASS_SECONDS,
)


def run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _owner_hash() -> str:
    return _web_auth.current_owner_hash(session)


def _owns(task: dict[str, Any]) -> bool:
    stored = task.get("owner_hash")
    if stored is None and _web_auth.mode == "local":
        return True
    return stored == _owner_hash()


def _owner_output_root() -> Path:
    if _web_auth.mode == "local":
        return OUTPUT_ROOT
    return OUTPUT_ROOT / "users" / _owner_hash()


def _task_manifest(run_id_value: str) -> Path:
    return RUNS_DIR / run_id_value / "task.json"


def _public_task(run_id_value: str, task: dict[str, Any]) -> dict[str, Any]:
    params = task.get("params", {})
    plan = task.get("plan", {}) or {}
    validation = task.get("validation") or {}
    return {
        "run_id": run_id_value,
        "status": task.get("status", "unknown"),
        "progress": float(task.get("progress", 0.0)),
        "started_at": task.get("started_at", ""),
        "finished_at": task.get("finished_at", ""),
        "workflow": params.get("workflow", ""),
        "grid_scheme": params.get("grid_scheme") if params.get("workflow") == "grid" else None,
        "bbox": params.get("bbox", []),
        "grid_count": len(params.get("grid_ids", [])),
        "grid_ids": list(params.get("grid_ids", [])),
        "years": list(params.get("years", [])),
        "sample_count": params.get("sample_count"),
        "source_name": plan.get("source_name"),
        "source_crs": plan.get("source_crs"),
        "grid_crs": plan.get("grid_crs", []),
        "raw_gib": plan.get("raw_gib"),
        "output_dir": str(task.get("output_dir", "")),
        "download_state_dir": str(task.get("download_state_dir", "")),
        "staging_dir": str(task.get("staging_dir", "")) if task.get("staging_dir") else "",
        "exit_code": task.get("exit_code"),
        "error": task.get("error"),
        "pid": task.get("pid"),
        "queue_position": _queue_position(run_id_value),
        "stage": task.get("stage", task.get("status", "unknown")),
        "progress_detail": task.get("progress_detail"),
        "resource_status": {
            "memory_reservation_bytes": task.get("memory_reservation_bytes"),
            "request_retries": task.get("request_retries", 0),
            "request_timeouts": task.get("request_timeouts", 0),
            "global_request_limit": GLOBAL_REQUEST_LIMIT,
            "profile": RESOURCE_PROFILE,
            "admission": _memory_admission.snapshot(),
        },
        "authentication": {
            "source": (task.get("auth_binding") or {}).get("auth_source", "auto"),
            "credential_version": (task.get("auth_binding") or {}).get(
                "credential_version", ""
            ),
            "project_fingerprint": (task.get("auth_binding") or {}).get(
                "project_fingerprint", ""
            ),
        },
        "validation": (
            {
                "valid": validation.get("valid"),
                "errors": validation.get("errors", []),
                "warnings": validation.get("warnings", []),
            }
            if validation
            else None
        ),
    }


def _queue_position(run_id_value: str) -> int | None:
    task = _tasks.get(run_id_value)
    if task is None or task.get("status") != "queued":
        return None
    queued = sorted(
        (
            (rid, value.get("started_at", ""))
            for rid, value in _tasks.items()
            if value.get("status") == "queued"
        ),
        key=lambda item: item[1],
    )
    for position, (rid, _) in enumerate(queued, 1):
        if rid == run_id_value:
            return position
    return None


def _persist_task(run_id_value: str) -> None:
    with _task_lock:
        task = _tasks.get(run_id_value)
        if task is None:
            return
        payload = {
            **_public_task(run_id_value, task),
            "params": task.get("params", {}),
            "plan": task.get("plan"),
            "hidden": bool(task.get("hidden", False)),
            "pid": task.get("pid"),
            "process_create_time": task.get("process_create_time"),
            "stage": task.get("stage"),
            "progress_detail": task.get("progress_detail"),
            "validation": task.get("validation"),
            "provenance": task.get("provenance"),
            "command": task.get("command"),
            "owner_hash": task.get("owner_hash"),
            "auth_binding": task.get("auth_binding"),
        }
    write_json(payload, _task_manifest(run_id_value))


def _matching_process(pid: Any, create_time: Any = None) -> psutil.Process | None:
    """Return a live process only when its persisted identity still matches."""
    try:
        process = psutil.Process(int(pid))
        if create_time is not None and abs(process.create_time() - float(create_time)) > 1.0:
            return None
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except (psutil.Error, TypeError, ValueError):
        return None


def _terminate_process_tree(
    pid: Any,
    create_time: Any = None,
    *,
    grace_seconds: float = 5.0,
) -> bool:
    """Terminate one exact process tree, but never signal a D-state member."""
    process = _matching_process(pid, create_time)
    if process is None:
        return True
    try:
        descendants = process.children(recursive=True)
        tree = [*descendants, process]
        for member in tree:
            try:
                if member.status() == psutil.STATUS_DISK_SLEEP:
                    return False
            except psutil.Error:
                continue
        for child in reversed(descendants):
            try:
                child.terminate()
            except psutil.Error:
                pass
        process.terminate()
        _, alive = psutil.wait_procs([*descendants, process], timeout=grace_seconds)
        for remaining in alive:
            try:
                remaining.kill()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(alive, timeout=max(1.0, grace_seconds))
        return not alive
    except psutil.Error:
        return _matching_process(pid, create_time) is None


def _load_tasks() -> None:
    for manifest in sorted(RUNS_DIR.glob("*/task.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            rid = str(payload["run_id"])
            status = str(payload.get("status", "interrupted"))
            recovery_note = None
            if status in {"running", "cancelling", "validating_output"}:
                pid = payload.get("pid")
                create_time = payload.get("process_create_time")
                if _matching_process(pid, create_time) is not None:
                    stopped = _terminate_process_tree(pid, create_time)
                    recovery_note = (
                        "Recovered after server restart; the orphan process tree was stopped. "
                        "Resubmit the same plan/output to resume."
                        if stopped
                        else "Recovered after server restart, but the old process could not be stopped."
                    )
            if status in {"running", "queued", "cancelling", "validating_output"}:
                status = "interrupted"
                payload["status"] = status
                payload["finished_at"] = _now()
                if recovery_note:
                    payload["error"] = recovery_note
            _tasks[rid] = {
                **payload,
                "status": status,
                "log_path": manifest.parent / "run.log",
                "output_dir": Path(payload.get("output_dir", OUTPUT_ROOT / rid)),
                "download_state_dir": Path(
                    payload.get("download_state_dir") or manifest.parent / "download_state"
                ),
                "staging_dir": (
                    Path(payload["staging_dir"]) if payload.get("staging_dir") else None
                ),
                "cancel_event": threading.Event(),
                "grid_progress": {},
                "owner_hash": payload.get("owner_hash"),
                "auth_binding": payload.get("auth_binding") or {},
            }
            if status == "interrupted":
                _persist_task(rid)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue


_load_tasks()


def _as_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return normalized


def _normalize_bbox(value: Any) -> list[float] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        try:
            value = [value["west"], value["south"], value["east"], value["north"]]
        except KeyError as exc:
            raise ValueError("bbox must contain west, south, east and north") from exc
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("bbox must be [west, south, east, north]")
    try:
        west, south, east, north = map(float, value)
    except (TypeError, ValueError) as exc:
        raise ValueError("bbox coordinates must be numeric") from exc
    if not all(map(math.isfinite, (west, south, east, north))):
        raise ValueError("bbox coordinates must be finite")
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must satisfy -180 <= west < east <= 180 and -90 <= south < north <= 90")
    return [west, south, east, north]


def _normalize_years(value: Any) -> list[int]:
    try:
        years = sorted(set(int(year) for year in value))
    except (TypeError, ValueError) as exc:
        raise ValueError("years must be a list of integers") from exc
    if not years:
        raise ValueError("Select at least one year")
    invalid = [year for year in years if year < AEF_FIRST_YEAR or year > AEF_LAST_YEAR]
    if invalid:
        raise ValueError(
            f"AEF supports {AEF_FIRST_YEAR}-{AEF_LAST_YEAR}; invalid years: {invalid}"
        )
    return years


def _geometry_from_request(aoi: Any, bbox: list[float] | None):
    from shapely.geometry import box, shape
    from shapely.ops import unary_union

    geometries = []
    if isinstance(aoi, dict):
        if aoi.get("type") == "FeatureCollection":
            geometries = [
                shape(feature["geometry"])
                for feature in aoi.get("features", [])
                if feature.get("geometry")
            ]
        elif aoi.get("type") == "Feature" and aoi.get("geometry"):
            geometries = [shape(aoi["geometry"])]
        elif aoi.get("type"):
            geometries = [shape(aoi)]
    if geometries:
        geometry = unary_union(geometries)
        if geometry.is_empty:
            raise ValueError("AOI geometry is empty")
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("AOI geometry is invalid")
        west, south, east, north = geometry.bounds
        if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("AOI must be normalized to WGS84 longitude/latitude coordinates")
        if east - west > 180:
            raise ValueError("AOIs crossing the antimeridian are not supported by the web planner")
        return geometry
    if bbox is not None:
        return box(*bbox)
    raise ValueError("Draw/import an AOI or provide a bbox")


def _grid_specs(params: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    from pyproj import Transformer
    from shapely.geometry import box, mapping
    from shapely.ops import transform
    from shapely import wkt

    scheme = params["grid_scheme"]
    if scheme == "reference":
        from aef_grits.grids import ReferenceGridProvider

        reference = Path(params["reference_path"]).expanduser().resolve()
        if not reference.exists():
            raise ValueError(f"Reference grid does not exist: {reference}")
        spec = ReferenceGridProvider(reference, params["tile_id"]).get()
        projected = box(*spec.bounds)
        transformer = Transformer.from_crs(spec.crs, "EPSG:4326", always_xy=True)
        geographic = transform(transformer.transform, projected)
        return [spec], [{
            "grid_id": spec.grid_id,
            "crs": str(spec.crs),
            "footprint": mapping(geographic),
        }]

    geometry = _geometry_from_request(params.get("aoi"), params.get("bbox"))
    region = {
        "region_id": "web_aoi",
        "region_name_cn": "Web AOI",
        "region_name_en": "Web AOI",
        "geometry": geometry,
    }
    if scheme == "mgrs":
        rows = resolve_mgrs([region])
        ids = sorted({row["grid_id"] for row in rows})
        specs = MGRSGridProvider().get_many(ids)
        by_id = {row["grid_id"]: row for row in rows}
        selections = [
            {
                "grid_id": grid_id,
                "crs": f"EPSG:{int(by_id[grid_id]['utm_epsg'])}",
                "footprint": mapping(wkt.loads(by_id[grid_id]["geographic_wkt"])),
            }
            for grid_id in ids
        ]
    elif scheme == "tessera_0p1":
        rows = resolve_tessera([region])
        selections = sorted(
            {
                (row["grid_id"], row["tile_center_lon"], row["tile_center_lat"])
                for row in rows
            }
        )
        provider = Tessera01GridProvider()
        specs = [provider.get(lon, lat, coordinates_are_centres=True) for _, lon, lat in selections]
        selections = [
            {
                "grid_id": grid_id,
                "lon": float(lon),
                "lat": float(lat),
                "crs": str(spec.crs),
                "footprint": mapping(box(lon - 0.05, lat - 0.05, lon + 0.05, lat + 0.05)),
            }
            for (grid_id, lon, lat), spec in zip(selections, specs)
        ]
    else:
        raise ValueError("grid_scheme must be mgrs, tessera_0p1 or reference")
    if not specs:
        raise ValueError("AOI does not intersect any grid")
    if len(specs) > MAX_GRID_COUNT:
        raise ValueError(
            f"AOI intersects {len(specs)} grids, exceeding the local safety limit "
            f"of {MAX_GRID_COUNT}; reduce the AOI or raise AEF_GRITS_WEB_MAX_GRIDS"
        )
    return specs, selections


def _normalize_params(body: dict[str, Any]) -> dict[str, Any]:
    workflow = str(body.get("workflow", "grid")).strip().lower()
    if workflow == "point":
        workflow = "points"
    if workflow not in {"grid", "points"}:
        raise ValueError("workflow must be grid or points")
    project = str(body.get("project", "")).strip()
    if not project:
        raise ValueError("Google Earth Engine project is required")
    params = {
        "project": project,
        "workflow": workflow,
        "years": _normalize_years(body.get("years", [])),
        "bbox": _normalize_bbox(body.get("bbox")),
        "aoi": body.get("aoi"),
        "grid_scheme": (
            str(body.get("gridScheme") or body.get("grid_scheme") or "tessera_0p1")
            if workflow == "grid"
            else None
        ),
        "block_size": _as_int(body.get("blockSize") or body.get("block_size"), "block_size", 256, 16, 433),
        "checkpoint_every": _as_int(body.get("checkpointInterval") or body.get("checkpoint_every"), "checkpoint_every", 8, 1, 10000),
        "max_retries": _as_int(body.get("maxRetries") or body.get("max_retries"), "max_retries", 6, 0, 20),
        "request_timeout_seconds": _as_int(
            body.get("requestTimeoutSeconds") or body.get("request_timeout_seconds"),
            "request_timeout_seconds",
            300,
            10,
            3600,
        ),
        "workers": _as_int(body.get("workers"), "workers", 2 if workflow == "grid" else 1, 1, 8),
        "chunk_size": _as_int(body.get("chunkSize") or body.get("chunk_size"), "chunk_size", 1000, 1, 10000),
        "page_size": _as_int(body.get("pageSize") or body.get("page_size"), "page_size", 1000, 1, 10000),
        "output_name": str(body.get("output") or "").strip(),
        "reference_path": str(body.get("referencePath") or body.get("reference_path") or "").strip(),
        "tile_id": str(body.get("tileId") or body.get("tile_id") or "").strip(),
        "id_field": str(body.get("pointIdField") or body.get("id_field") or "").strip(),
        "layer": str(body.get("layer") or "").strip(),
        "geometry_mode": str(body.get("geometryMode") or body.get("geometry_mode") or "auto"),
        "reference_grid": str(body.get("referenceGrid") or body.get("reference_grid") or "").strip(),
        "max_points": _as_int(body.get("maxPoints") or body.get("max_points"), "max_points", 1_000_000, 1, 10_000_000),
    }
    if params["workflow"] == "grid":
        if params["grid_scheme"] not in {"mgrs", "tessera_0p1", "reference"}:
            raise ValueError("Unknown grid scheme")
        if params["grid_scheme"] == "reference" and not (
            params["reference_path"] and params["tile_id"]
        ):
            raise ValueError("Reference mode requires a reference path and tile ID")
        if params["grid_scheme"] != "reference" and not (params["aoi"] or params["bbox"]):
            raise ValueError("MGRS and Tessera modes require an AOI or bbox")
    elif params["geometry_mode"] not in {"auto", "representative", "centroid", "interior_pixels"}:
        raise ValueError("Invalid point geometry mode")
    return params


def _plan_grid(params: dict[str, Any]) -> dict[str, Any]:
    from shapely.geometry import mapping

    specs, selections = _grid_specs(params)
    aoi_geometry = None
    if params["grid_scheme"] != "reference":
        aoi_geometry = _geometry_from_request(params.get("aoi"), params.get("bbox"))
    raw_bytes = sum(
        len(params["years"]) * 64 * int(spec.width) * int(spec.height) * 4
        for spec in specs
    )
    params["grid_ids"] = [selection["grid_id"] for selection in selections]
    params["grid_selections"] = selections
    resources = plan_grid_resources(
        block_size=params["block_size"],
        workers=params["workers"],
        snapshot=WEB_MEMORY_SNAPSHOT,
        global_request_limit=GLOBAL_REQUEST_LIMIT,
    )
    params["resolved_workers"] = resources.workers
    return {
        "workflow": "grid",
        "grid_scheme": params["grid_scheme"],
        "grid_count": len(specs),
        "grid_ids": params["grid_ids"],
        "grid_features": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"grid_id": item["grid_id"], "crs": item["crs"]},
                    "geometry": item["footprint"],
                }
                for item in selections
            ],
        },
        "grid_crs": sorted({item["crs"] for item in selections}),
        "aoi_crs": "EPSG:4326",
        "aoi_geometry": mapping(aoi_geometry) if aoi_geometry is not None else None,
        "resolution_m": 10.0,
        "years": params["years"],
        "raw_bytes": raw_bytes,
        "raw_gib": raw_bytes / 1024**3,
        "note": "Storage after lossless compression depends on local feature entropy.",
        "resource_plan": resources.as_dict(),
        "memory": WEB_MEMORY_SNAPSHOT.as_dict(),
    }


def _safe_output_dir(name: str, rid: str) -> Path:
    root = _owner_output_root()
    if not name:
        return root / rid
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Output must be a relative folder name under the configured output root")
    resolved = Path(os.path.abspath(root / candidate))
    if resolved == root or os.path.commonpath([resolved, root]) != str(root):
        raise ValueError("Output folder must stay inside the configured output root")
    return resolved


def _safe_output_browser_dir(name: str = "") -> Path:
    """Resolve a browsable directory while confining it to OUTPUT_ROOT."""
    root = _owner_output_root()
    candidate = Path(name or ".")
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Output browser path must be relative to the configured output root")
    resolved = Path(os.path.abspath(root / candidate))
    if os.path.commonpath([resolved, root]) != str(root):
        raise ValueError("Output browser path must stay inside the configured output root")
    return resolved


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _provenance() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    packages = {
        name: _package_version(name)
        for name in ("aef-grits", "earthengine-api", "flask", "geopandas", "numpy", "pandas", "pyproj", "rasterio", "xarray", "zarr")
    }
    return {
        "created_at": _now(),
        "git_commit": commit,
        "git_dirty": dirty,
        "python": sys.version,
        "python_executable": PYTHON_BIN,
        "platform": platform.platform(),
        "packages": packages,
        "dataset": DATASET,
        "aef_year_range": [AEF_FIRST_YEAR, AEF_LAST_YEAR],
        "aef_dimensions": 64,
        "resolution_m": 10.0,
        "mgrs_index": str(BUILTIN_MGRS_INDEX.resolve()),
        "mgrs_index_sha256": _file_sha256(BUILTIN_MGRS_INDEX),
    }


def _capacity_plan(plan: dict[str, Any]) -> dict[str, Any]:
    raw_bytes = int(plan.get("raw_bytes", 0))
    disk_required = int(raw_bytes * DISK_FACTOR) + DISK_RESERVE_BYTES
    validate_storage_layout(OUTPUT_ROOT, RUNS_DIR, STAGING_ROOT)
    incident = RUNS_DIR / "filesystem_incident.json"
    free_bytes = isolated_disk_free(
        OUTPUT_ROOT, timeout_seconds=10, incident_path=incident
    )
    staging_free = (
        isolated_disk_free(STAGING_ROOT, timeout_seconds=10, incident_path=incident)
        if STAGING_ROOT is not None
        else free_bytes
    )
    blocked_reasons = []
    if raw_bytes > MAX_RAW_BYTES:
        blocked_reasons.append(
            f"Raw size {raw_bytes / 1024**3:.2f} GiB exceeds the hard limit "
            f"of {MAX_RAW_BYTES / 1024**3:.2f} GiB"
        )
    if free_bytes < disk_required:
        blocked_reasons.append(
            f"Free disk {free_bytes / 1024**3:.2f} GiB is below the required "
            f"{disk_required / 1024**3:.2f} GiB"
        )
    if STAGING_ROOT is not None:
        per_grid_raw = max(
            (
                int(grid.get("width", 0))
                * int(grid.get("height", 0))
                * len(plan.get("years", []))
                * 64
                * 4
                for grid in plan.get("grids", [])
            ),
            default=raw_bytes,
        )
        staging_required = int(per_grid_raw * DISK_FACTOR) + DISK_RESERVE_BYTES
        if staging_free < staging_required:
            blocked_reasons.append(
                f"Staging free space {staging_free / 1024**3:.2f} GiB is below "
                f"the largest-grid requirement {staging_required / 1024**3:.2f} GiB"
            )
    else:
        staging_required = disk_required
    strong_confirmation = raw_bytes >= WARN_RAW_BYTES
    phrase = f"DOWNLOAD {raw_bytes / 1024**3:.2f} GiB" if strong_confirmation else ""
    return {
        **plan,
        "disk_free_bytes": free_bytes,
        "disk_free_gib": free_bytes / 1024**3,
        "disk_required_bytes": disk_required,
        "disk_required_gib": disk_required / 1024**3,
        "staging_free_bytes": staging_free,
        "staging_free_gib": staging_free / 1024**3,
        "staging_required_bytes": staging_required,
        "staging_required_gib": staging_required / 1024**3,
        "storage_layout": STORAGE_LAYOUT.as_dict(),
        "hard_limit_bytes": MAX_RAW_BYTES,
        "hard_limit_gib": MAX_RAW_BYTES / 1024**3,
        "can_start": not blocked_reasons,
        "blocked_reasons": blocked_reasons,
        "strong_confirmation": strong_confirmation,
        "confirmation_phrase": phrase,
    }


def _write_plan(
    params: dict[str, Any],
    plan: dict[str, Any],
    plan_dir: Path | None = None,
    *,
    auth_binding: AuthBinding | None = None,
) -> dict[str, Any]:
    plan_dir = plan_dir or (PLANS_DIR / uuid.uuid4().hex)
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_id_value = plan_dir.name
    enriched = _capacity_plan(plan)
    binding = auth_binding or _web_auth.current_binding(
        session, params["project"], verify=False
    )
    record = {
        "plan_id": plan_id_value,
        "created_at": _now(),
        "status": "ready",
        "params": params,
        "plan": enriched,
        "owner_hash": binding.owner_hash,
        "auth_binding": binding.as_dict(),
    }
    digest = _json_digest(record)
    record["digest"] = digest
    write_json(record, plan_dir / "plan.json")
    token = _plan_serializer.dumps({"plan_id": plan_id_value, "digest": digest})
    return {**enriched, "plan_id": token, "expires_in_seconds": PLAN_TTL_SECONDS}


def _read_plan(token: str, confirmation: str = "") -> tuple[Path, dict[str, Any]]:
    if not token:
        raise ValueError("A signed plan_id is required; run preflight first")
    try:
        signed = _plan_serializer.loads(token, max_age=PLAN_TTL_SECONDS)
    except SignatureExpired as exc:
        raise ValueError("The plan has expired; run preflight again") from exc
    except BadSignature as exc:
        raise ValueError("Invalid plan_id") from exc
    plan_dir = PLANS_DIR / str(signed.get("plan_id", ""))
    plan_path = plan_dir / "plan.json"
    if not plan_path.exists():
        raise ValueError("Plan state is missing; run preflight again")
    record = json.loads(plan_path.read_text(encoding="utf-8"))
    if record.get("owner_hash") != _owner_hash():
        raise PermissionError("This plan belongs to another user")
    if record.get("status") != "ready":
        raise ValueError(f"Plan is not reusable (status={record.get('status')})")
    digest = record.pop("digest", None)
    if digest != signed.get("digest") or digest != _json_digest(record):
        raise ValueError("Plan state signature mismatch")
    record["digest"] = digest
    capacity = _capacity_plan(record["plan"])
    if not capacity["can_start"]:
        raise ValueError("; ".join(capacity["blocked_reasons"]))
    if capacity["strong_confirmation"] and confirmation.strip() != capacity["confirmation_phrase"]:
        raise ValueError(
            f"Large task confirmation must exactly match: {capacity['confirmation_phrase']}"
        )
    record["plan"] = capacity
    current = _web_auth.current_binding(
        session, record["params"]["project"], verify=False
    )
    expected = record.get("auth_binding") or {}
    if (
        current.owner_hash != expected.get("owner_hash")
        or current.credential_version != expected.get("credential_version")
        or current.project_fingerprint != expected.get("project_fingerprint")
    ):
        raise ValueError(
            "The login, credential version, or project changed after planning; run preflight again"
        )
    return plan_dir, record


def _consume_plan(plan_dir: Path, record: dict[str, Any], rid: str) -> None:
    record["status"] = "consumed"
    record["consumed_at"] = _now()
    record["run_id"] = rid
    write_json(record, plan_dir / "plan.json")


def _build_cmd(
    params: dict[str, Any],
    output_dir: Path,
    *,
    state_dir: Path | None = None,
    staging_dir: Path | None = None,
) -> list[str]:
    years = [str(year) for year in params["years"]]
    if params["workflow"] == "grid":
        cmd = [
            PYTHON_BIN,
            str(ROOT / "scripts" / "stream_aef_grid_ee.py"),
            "--grid-scheme",
            params["grid_scheme"],
            "--auth-source",
            params.get("auth_source", "auto"),
            "--out-dir",
            str(output_dir),
            "--years",
            *years,
            "--block-size",
            str(params["block_size"]),
            "--workers",
            str(params.get("resolved_workers", params["workers"])),
            "--global-request-limit",
            str(GLOBAL_REQUEST_LIMIT),
            "--resource-profile",
            RESOURCE_PROFILE,
            "--resource-state-dir",
            str(RESOURCE_STATE_ROOT),
            "--max-retries",
            str(params["max_retries"]),
            "--request-timeout-seconds",
            str(params.get("request_timeout_seconds", 300)),
            "--checkpoint-every",
            str(params["checkpoint_every"]),
        ]
        if params["grid_scheme"] == "mgrs":
            cmd.extend(["--tiles", *params["grid_ids"]])
        elif params["grid_scheme"] == "tessera_0p1":
            for selection in params["grid_selections"]:
                cmd.extend(["--tessera-tile", str(selection["lon"]), str(selection["lat"])])
        else:
            cmd.extend(["--reference", params["reference_path"], "--tile-id", params["tile_id"]])
        if state_dir is not None:
            cmd.extend(["--state-dir", str(state_dir)])
        if staging_dir is not None:
            cmd.extend(["--staging-dir", str(staging_dir)])
        return cmd

    cmd = [
        PYTHON_BIN,
        str(ROOT / "scripts" / "stream_aef_points_ee.py"),
        "--samples",
        params["samples_path"],
        "--auth-source",
        params.get("auth_source", "auto"),
        "--out-dir",
        str(output_dir),
        "--years",
        *years,
        "--workers",
        str(params.get("resolved_workers", params["workers"])),
        "--global-request-limit",
        str(GLOBAL_REQUEST_LIMIT),
        "--resource-profile",
        RESOURCE_PROFILE,
        "--resource-state-dir",
        str(RESOURCE_STATE_ROOT),
        "--max-retries",
        str(params["max_retries"]),
        "--request-timeout-seconds",
        str(params.get("request_timeout_seconds", 300)),
        "--chunk-size",
        str(params.get("chunk_size", 1000)),
        "--page-size",
        str(params.get("page_size", 1000)),
        "--geometry-mode",
        params["geometry_mode"],
        "--max-points",
        str(params["max_points"]),
    ]
    if params["id_field"]:
        cmd.extend(["--id-field", params["id_field"]])
    if params["layer"]:
        cmd.extend(["--layer", params["layer"]])
    if params["reference_grid"]:
        cmd.extend(["--reference-grid", params["reference_grid"]])
    if state_dir is not None:
        cmd.extend(["--state-dir", str(state_dir)])
    if staging_dir is not None:
        cmd.extend(["--staging-dir", str(staging_dir)])
    return cmd


def _update_progress(rid: str, line: str) -> None:
    if line.startswith(EVENT_PREFIX):
        try:
            event = json.loads(line[len(EVENT_PREFIX) :])
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict):
            with _task_lock:
                task = _tasks.get(rid)
                if task is None:
                    return
                task["progress_detail"] = event
                if event.get("event") == "request_retry":
                    task["request_retries"] = int(task.get("request_retries", 0)) + 1
                elif event.get("event") == "request_timeout":
                    task["request_timeouts"] = int(task.get("request_timeouts", 0)) + 1
                elif event.get("event") == "auth_required":
                    task["auth_failure"] = True
                grid_ids = task.get("params", {}).get("grid_ids", [])
                grid_id = event.get("grid_id")
                if event.get("event") == "grid_start" and grid_id:
                    task["current_grid"] = grid_id
                if event.get("event") == "grid_complete" and grid_id in grid_ids:
                    task.setdefault("grid_progress", {})[grid_id] = 1.0
                if event.get("event") == "progress":
                    completed = int(event.get("completed", 0))
                    total = int(event.get("total", 0))
                    fraction = max(0.0, min(1.0, completed / total)) if total else 0.0
                    if grid_ids and grid_id in grid_ids:
                        task.setdefault("grid_progress", {})[grid_id] = fraction
                        task["progress"] = sum(
                            task["grid_progress"].get(item, 0.0) for item in grid_ids
                        ) / len(grid_ids)
                    else:
                        task["progress"] = fraction
                old_bucket = task.get("progress_bucket", -1)
                new_bucket = int(float(task.get("progress", 0.0)) * 100)
                if new_bucket != old_bucket or event.get("event") != "progress":
                    task["progress_bucket"] = new_bucket
                    _persist_task(rid)
                events_path = Path(task["log_path"]).with_name("events.jsonl")
                with events_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            return
    grid_match = GRID_ID_PATTERN.search(line)
    if grid_match:
        with _task_lock:
            task = _tasks.get(rid)
            if task is not None and grid_match.group(1) in task.get("params", {}).get("grid_ids", []):
                task["current_grid"] = grid_match.group(1)
    match = PROGRESS_PATTERN.search(line)
    if not match:
        return
    completed, total = map(int, match.groups())
    if total <= 0:
        return
    with _task_lock:
        task = _tasks.get(rid)
        if task is None:
            return
        fraction = max(0.0, min(1.0, completed / total))
        grid_ids = task.get("params", {}).get("grid_ids", [])
        current_grid = task.get("current_grid")
        if grid_ids and current_grid in grid_ids:
            task.setdefault("grid_progress", {})[current_grid] = fraction
            task["progress"] = sum(
                task["grid_progress"].get(grid_id, 0.0) for grid_id in grid_ids
            ) / len(grid_ids)
        else:
            task["progress"] = fraction
        old_bucket = task.get("progress_bucket", -1)
        new_bucket = int(task["progress"] * 100)
        if new_bucket != old_bucket:
            task["progress_bucket"] = new_bucket
            _persist_task(rid)


def _validate_task_outputs_in_process(task: dict[str, Any]) -> dict[str, Any]:
    """Validate lightweight structural invariants without loading raster values."""
    import pandas as pd

    output_dir = Path(task["output_dir"]).resolve()
    control_dir = Path(task.get("download_state_dir") or output_dir).resolve()
    params = task["params"]
    errors: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {"workflow": params["workflow"], "output_dir": str(output_dir)}
    if params["workflow"] == "points":
        report_path = control_dir / "report.json"
        catalog_path = control_dir / "catalog.parquet"
        if not report_path.exists():
            errors.append("Missing point report.json")
        if not catalog_path.exists():
            errors.append("Missing point catalog.parquet")
        if not errors:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            catalog = pd.read_parquet(catalog_path)
            expected = int(params.get("sample_count", 0))
            complete = int(report.get("complete_rows", -1))
            incomplete = int(report.get("incomplete_rows", -1))
            if int(report.get("sample_count", -1)) != expected:
                errors.append("Point report sample_count differs from the accepted plan")
            if complete + incomplete != expected:
                errors.append("Point complete/incomplete counts do not sum to sample_count")
            if incomplete:
                warnings.append(f"{incomplete} point rows have incomplete AEF features")
            shard_paths = [Path(value) for value in catalog.get("path", [])]
            missing_shards = [str(path) for path in shard_paths if not path.exists()]
            if missing_shards:
                errors.append(f"Missing {len(missing_shards)} point shards")
            summary.update(
                {
                    "sample_count": expected,
                    "complete_rows": complete,
                    "incomplete_rows": incomplete,
                    "shards": len(catalog),
                    "feature_count": len(params["years"]) * 64,
                    "catalog": str(catalog_path),
                    "report": str(report_path),
                }
            )
    else:
        import xarray as xr

        report_path = control_dir / "run_summary.json"
        catalog_path = control_dir / "catalog.parquet"
        if not report_path.exists():
            errors.append("Missing grid run_summary.json")
        if not catalog_path.exists():
            errors.append("Missing grid catalog.parquet")
        grids = []
        if not errors:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            catalog = pd.read_parquet(catalog_path)
            expected_ids = set(params.get("grid_ids", []))
            actual_ids = set(catalog.grid_id.astype(str)) if "grid_id" in catalog else set()
            if expected_ids != actual_ids:
                errors.append(
                    f"Catalog grid IDs differ from plan: expected={sorted(expected_ids)}, actual={sorted(actual_ids)}"
                )
            if "status" not in catalog or not catalog.status.astype(str).eq("complete").all():
                errors.append("Not every grid catalog row is complete")
            if int(report.get("completed", -1)) != len(expected_ids) or report.get("failed"):
                errors.append("Grid run summary is incomplete")
            for row in catalog.itertuples(index=False):
                zarr_path = Path(str(row.zarr_path)).resolve()
                if output_dir not in zarr_path.parents:
                    errors.append(f"Catalog path escapes task output directory: {zarr_path}")
                    continue
                if not zarr_path.exists():
                    errors.append(f"Missing Zarr store: {zarr_path}")
                    continue
                try:
                    dataset = xr.open_zarr(zarr_path, consolidated=True)
                    array = dataset["embeddings"]
                    if array.ndim != 4 or array.shape[0] != len(params["years"]) or array.shape[1] != 64:
                        errors.append(f"Unexpected embedding shape for {row.grid_id}: {array.shape}")
                    grids.append(
                        {
                            "grid_id": str(row.grid_id),
                            "zarr_path": str(zarr_path),
                            "shape": list(array.shape),
                            "chunks": [list(chunk) for chunk in array.chunks],
                            "crs": str(row.crs),
                            "resolution_m": float(row.resolution_m),
                            "compression": {
                                "codec": str(row.compression_codec),
                                "level": int(row.compression_level),
                                "shuffle": str(row.compression_shuffle),
                            },
                        }
                    )
                    dataset.close()
                except Exception as exc:
                    errors.append(f"Cannot open {zarr_path}: {exc}")
            summary.update(
                {
                    "grid_count": len(expected_ids),
                    "grids": grids,
                    "catalog": str(catalog_path),
                    "report": str(report_path),
                }
            )
    return {
        **summary,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "validated_at": _now(),
    }


def _validate_task_outputs(task: dict[str, Any]) -> dict[str, Any]:
    """Validate final products in a disposable process, never a Web thread."""
    state_dir = Path(task.get("download_state_dir") or task["output_dir"])
    state_dir.mkdir(parents=True, exist_ok=True)
    input_path = state_dir / "validation_input.json"
    result_path = state_dir / "validation_result.json"
    output_dir = Path(task["output_dir"]).absolute()
    write_json(
        {
            "output_dir": str(output_dir),
            "control_dir": str(state_dir.absolute()),
            "params": task["params"],
        },
        input_path,
    )
    command = [
        PYTHON_BIN,
        "-m",
        "aef_grits.delivery_validation",
        "--input",
        str(input_path),
        "--output",
        str(result_path),
        "--final",
        str(output_dir),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
        start_new_session=os.name == "posix",
    )
    started = time.monotonic()
    while process.poll() is None:
        if time.monotonic() - started > VALIDATION_TIMEOUT_SECONDS:
            incident = {
                "workflow": task["params"]["workflow"],
                "output_dir": str(output_dir),
                "valid": False,
                "errors": [
                    "Product validation timed out in an isolated process; "
                    "stop new work and inspect the final filesystem"
                ],
                "warnings": [],
                "validation_pid": process.pid,
                "validated_at": _now(),
            }
            write_json(incident, result_path)
            terminate_process_group_once(process)
            return incident
        time.sleep(0.1)
    _, stderr = process.communicate()
    if process.returncode:
        return {
            "workflow": task["params"]["workflow"],
            "output_dir": str(output_dir),
            "valid": False,
            "errors": [f"Isolated product validation failed: {stderr.strip()}"],
            "warnings": [],
            "validated_at": _now(),
        }
    return json.loads(result_path.read_text(encoding="utf-8"))


def _run_subprocess(rid: str, cmd: list[str], log_path: Path) -> None:
    admitted_bytes = 0
    try:
        with _task_lock:
            task = _tasks[rid]
            cancel_event: threading.Event = task["cancel_event"]
            if cancel_event.is_set():
                task["status"] = "cancelled"
                task["stage"] = "cancelled"
                task["finished_at"] = _now()
                _persist_task(rid)
                return
            admitted_bytes = int(
                (task.get("plan", {}).get("resource_plan") or {}).get(
                    "estimated_peak_bytes", 256 * 1024**2
                )
            )
            task["status"] = "queued"
            task["stage"] = "waiting_resources"
            task["memory_reservation_bytes"] = admitted_bytes
            _persist_task(rid)
        if not _memory_admission.acquire(admitted_bytes, cancel_event, request_id=rid):
            with _task_lock:
                task = _tasks[rid]
                task["status"] = "cancelled"
                task["stage"] = "cancelled"
                task["finished_at"] = _now()
                _persist_task(rid)
            admitted_bytes = 0
            return
        with _task_lock:
            task = _tasks[rid]
            task["status"] = "running"
            task["stage"] = "downloading"
            _persist_task(rid)
        child_env = dict(os.environ)
        binding = task.get("auth_binding") or {}
        child_env["AEF_GRITS_AUTH_SOURCE"] = str(
            binding.get("auth_source") or task["params"].get("auth_source") or "auto"
        )
        project = str(task["params"].get("project") or "").strip()
        if project:
            child_env["AEF_GRITS_PROJECT"] = project
        else:
            child_env.pop("AEF_GRITS_PROJECT", None)
        handle = str(binding.get("credential_handle") or "")
        if handle:
            child_env["AEF_GRITS_CREDENTIAL_HANDLE"] = handle
        else:
            child_env.pop("AEF_GRITS_CREDENTIAL_HANDLE", None)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("COMMAND: " + subprocess.list2cmdline(cmd) + "\n")
                stream.flush()
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                    creationflags=creationflags,
                )
                with _task_lock:
                    _processes[rid] = process
                    task["pid"] = process.pid
                    task["process_create_time"] = psutil.Process(process.pid).create_time()
                    _persist_task(rid)
                for raw_line in process.stdout or ():
                    line = raw_line.rstrip("\r\n")
                    stream.write(line + "\n")
                    stream.flush()
                    _update_progress(rid, line)
                return_code = process.wait()
            with _task_lock:
                task = _tasks[rid]
                task["exit_code"] = return_code
                task["pid"] = None
                task["process_create_time"] = None
                if task["status"] in {"cancelling", "cancelled"} or cancel_event.is_set():
                    task["status"] = "cancelled"
                    task["stage"] = "cancelled"
                    task["finished_at"] = _now()
                    _persist_task(rid)
                elif return_code == 0:
                    task["status"] = "validating_output"
                    task["stage"] = "validating_output"
                    _persist_task(rid)
                elif task.get("auth_failure"):
                    task["status"] = "auth_required"
                    task["stage"] = "auth_required"
                    task["error"] = (
                        "Stored authorization or project access must be renewed; "
                        "the download checkpoint was preserved."
                    )
                    task["finished_at"] = _now()
                    _persist_task(rid)
                else:
                    task["status"] = "failed"
                    task["stage"] = "failed"
                    task["finished_at"] = _now()
                    _persist_task(rid)
            if return_code == 0 and not cancel_event.is_set():
                validation = _validate_task_outputs(task)
                web_report = {
                    "run_id": rid,
                    "params": task["params"],
                    "plan": task.get("plan"),
                    "validation": validation,
                    "provenance": task.get("provenance"),
                    "command": task.get("command"),
                }
                report_path = Path(task["download_state_dir"]) / "web_task_report.json"
                write_json(web_report, report_path)
                with _task_lock:
                    task = _tasks[rid]
                    task["validation"] = validation
                    task["finished_at"] = _now()
                    if validation["valid"]:
                        task["status"] = "done"
                        task["stage"] = "done"
                        task["progress"] = 1.0
                    else:
                        task["status"] = "failed"
                        task["stage"] = "validation_failed"
                        task["error"] = "; ".join(validation["errors"])
                    _persist_task(rid)
        except Exception as exc:  # pragma: no cover - defensive runner boundary
            with _task_lock:
                task = _tasks.get(rid)
                if task is not None:
                    task["status"] = "cancelled" if cancel_event.is_set() else "error"
                    task["stage"] = task["status"]
                    task["error"] = str(exc)
                    task["finished_at"] = _now()
                    task["pid"] = None
                    task["process_create_time"] = None
                    _persist_task(rid)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"ERROR: {exc}\n")
        finally:
            with _task_lock:
                _processes.pop(rid, None)
    finally:
        if admitted_bytes:
            _memory_admission.release(admitted_bytes)
        _submission_slots.release()


def _request_body() -> dict[str, Any]:
    if request.mimetype == "multipart/form-data":
        try:
            body = json.loads(request.form.get("payload", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("Multipart payload must contain valid JSON") from exc
    else:
        body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def _save_point_uploads(run_dir: Path) -> Path:
    uploads = request.files.getlist("samples")
    uploads = [upload for upload in uploads if upload and upload.filename]
    if not uploads:
        raise ValueError("Point workflow requires one or more sample files")
    input_dir = run_dir / "input"
    input_dir.mkdir(parents=True)
    primary: Path | None = None
    for upload in uploads:
        original = Path(upload.filename.replace("\\", "/")).name
        suffix = Path(original).suffix.lower()
        stem = secure_filename(Path(original).stem)
        if not stem:
            stem = hashlib.sha256(Path(original).stem.encode("utf-8")).hexdigest()[:12]
        name = stem + suffix
        target = input_dir / name
        upload.save(target)
        if target.suffix.lower() in POINT_SUFFIXES:
            if primary is None or target.suffix.lower() == ".shp":
                primary = target
    if primary is None:
        raise ValueError("No supported point source found in upload")
    return primary


def _point_source_crs(path: Path, layer: str | None = None) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "EPSG:4326 (lon/lat columns)"
    try:
        import pyogrio

        info = pyogrio.read_info(path, layer=layer)
        return str(info.get("crs") or "missing/undefined")
    except Exception:
        return "EPSG:4326 (lon/lat table)" if suffix == ".parquet" else "unavailable"


def _inspect_point_geometry(path: Path, layer: str | None = None) -> list[str]:
    """Require vector point inputs to contain only Point/MultiPoint features."""
    if path.suffix.lower() not in {".shp", ".gpkg", ".geojson", ".json"}:
        return []
    import pyogrio

    info = pyogrio.read_info(path, layer=layer)
    if int(info.get("features") or 0) == 0:
        raise ValueError("Point vector is empty")
    if not info.get("crs"):
        raise ValueError("Point vector has no CRS; a Shapefile must include its .prj file")
    geometry_type = str(info.get("geometry_type") or "Unknown")
    if geometry_type.endswith(" Z"):
        geometry_type = geometry_type[:-2]
    geometry_types = [geometry_type]
    invalid = [name for name in geometry_types if name not in {"Point", "MultiPoint"}]
    if invalid:
        raise ValueError(
            "Point download accepts only Point/MultiPoint geometry; found "
            + ", ".join(invalid)
            + ". Convert polygons to points before uploading."
        )
    return geometry_types


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract an uploaded vector archive without allowing path traversal."""
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (destination / member.filename).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise ValueError("AOI ZIP contains an unsafe path")
        source.extractall(destination)


def _inspect_aoi_upload() -> dict[str, Any]:
    """Read an AOI upload and return authoritative WGS84 polygon GeoJSON."""
    uploads = [item for item in request.files.getlist("aoi") if item and item.filename]
    if not uploads:
        raise ValueError("Choose a Shapefile/ZIP or WKT file")
    with tempfile.TemporaryDirectory(prefix="aef_grits_aoi_", dir=RUNS_DIR) as temporary:
        temp_dir = Path(temporary)
        saved: list[Path] = []
        for upload in uploads:
            original = Path(upload.filename.replace("\\", "/")).name
            suffix = Path(original).suffix.lower()
            stem = secure_filename(Path(original).stem) or uuid.uuid4().hex[:12]
            target = temp_dir / (stem + suffix)
            upload.save(target)
            saved.append(target)

        wkt_files = [path for path in saved if path.suffix.lower() == ".wkt"]
        zip_files = [path for path in saved if path.suffix.lower() == ".zip"]
        if wkt_files:
            if len(saved) != 1:
                raise ValueError("Upload one WKT file at a time")
            from shapely import wkt
            from shapely.geometry import mapping

            geometry = wkt.loads(wkt_files[0].read_text(encoding="utf-8-sig").strip())
            source_crs = "EPSG:4326 (WKT file convention)"
            feature_count = 1
        else:
            for archive in zip_files:
                _safe_extract_zip(archive, temp_dir)
            candidates = sorted(temp_dir.rglob("*.shp"))
            if not candidates:
                candidates = [
                    path for path in saved if path.suffix.lower() in {".geojson", ".json", ".gpkg"}
                ]
            if not candidates:
                raise ValueError(
                    "AOI upload must be a .wkt file, a complete Shapefile component set, or a Shapefile ZIP"
                )
            import geopandas as gpd

            frame = gpd.read_file(candidates[0])
            if frame.empty:
                raise ValueError("AOI vector is empty")
            if frame.crs is None:
                raise ValueError("AOI vector has no CRS; a Shapefile must include its .prj file")
            source_crs = frame.crs.to_string()
            feature_count = len(frame)
            frame = frame.to_crs("EPSG:4326")
            geometry = frame.geometry.union_all()

        if geometry is None or geometry.is_empty:
            raise ValueError("AOI geometry is empty")
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError(
                f"Grid AOI must contain Polygon/MultiPolygon geometry; found {geometry.geom_type}"
            )
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("AOI geometry is invalid")
        west, south, east, north = map(float, geometry.bounds)
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("AOI is outside valid WGS84 longitude/latitude bounds after reprojection")
        from shapely.geometry import mapping

        return {
            "geometry": mapping(geometry),
            "source_crs": source_crs,
            "normalized_crs": "EPSG:4326",
            "feature_count": int(feature_count),
            "geometry_type": geometry.geom_type,
            "bbox": [west, south, east, north],
        }


@app.before_request
def reject_cross_origin_mutations():
    """Prevent external pages from mutating either local or shared runners."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    origin = request.headers.get("Origin")
    if not origin:
        return None
    parsed = urlsplit(origin)
    if parsed.netloc != request.host or parsed.scheme != request.scheme:
        return jsonify({"error": "Cross-origin task mutations are not allowed"}), 403
    return None


@app.before_request
def require_web_identity():
    if _web_auth.mode != "oauth" or not request.path.startswith("/api/"):
        return None
    public = {
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/callback",
    }
    if request.path in public:
        return None
    if not _web_auth.status(session).get("authenticated"):
        return jsonify({"error": "Google/Earth Engine login is required"}), 401
    return None


@app.after_request
def secure_response_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/auth/"):
        response.headers["Cache-Control"] = "no-store"
    if _web_auth.mode == "oauth" and not _web_auth.allow_insecure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(exc):
    return jsonify({"error": f"Upload exceeds the {MAX_UPLOAD_BYTES / 1024**2:.0f} MiB limit"}), 413


@app.errorhandler(HTTPException)
def http_error(exc):
    return jsonify({"error": exc.description, "status": exc.code}), exc.code


@app.errorhandler(Exception)
def unexpected_error(exc):  # pragma: no cover - Flask defensive boundary
    error_id = uuid.uuid4().hex[:12]
    print(
        f"Unexpected web error {error_id}: {redact_text(exc)}",
        file=sys.stderr,
        flush=True,
    )
    return jsonify({"error": "Internal server error", "error_id": error_id}), 500


@app.get("/")
def index() -> Response:
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/auth/status")
def auth_status() -> Response:
    return jsonify(_web_auth.status(session))


@app.get("/api/auth/login")
def auth_login() -> Response:
    try:
        return redirect(_web_auth.authorization_url(session))
    except (OSError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/auth/callback")
def auth_callback() -> Response:
    try:
        _web_auth.complete_callback(session, request.url)
        return redirect("/")
    except (OSError, RuntimeError, ValueError, PermissionError) as exc:
        return jsonify({"error": str(exc)}), 403


@app.post("/api/auth/logout")
def auth_logout() -> Response:
    _web_auth.logout(session)
    return jsonify({"ok": True})


@app.get("/api/capabilities")
def capabilities() -> Response:
    return jsonify(
        {
            "years": list(range(AEF_FIRST_YEAR, AEF_LAST_YEAR + 1)),
            "grid_schemes": ["mgrs", "tessera_0p1", "reference"],
            "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
            "max_queued_tasks": MAX_QUEUED_TASKS,
            "max_bypass_seconds": MAX_BYPASS_SECONDS,
            "global_request_limit": GLOBAL_REQUEST_LIMIT,
            "resource_profile": RESOURCE_PROFILE,
            "memory": WEB_MEMORY_SNAPSHOT.as_dict(),
            "resource_admission": _memory_admission.snapshot(),
            "resource_state_root": str(RESOURCE_STATE_ROOT),
            "max_grid_count": MAX_GRID_COUNT,
            "max_raw_gib": MAX_RAW_BYTES / 1024**3,
            "warn_raw_gib": WARN_RAW_BYTES / 1024**3,
            "plan_ttl_seconds": PLAN_TTL_SECONDS,
            "output_root": str(_owner_output_root()),
            "authentication_mode": _web_auth.mode,
            "python": PYTHON_BIN,
        }
    )


@app.get("/api/output-directories")
def output_directories() -> Response:
    """List server-side output folders without exposing paths outside the root."""
    try:
        if STORAGE_LAYOUT.final_mount and STORAGE_LAYOUT.final_mount.is_linux_ntfs:
            return jsonify(
                {
                    "output_root": str(_owner_output_root()),
                    "path": "",
                    "parent": None,
                    "directories": [],
                    "read_only": True,
                    "note": (
                        "Directory browsing is disabled for Linux NTFS output to keep "
                        "the web service out of uninterruptible kernel I/O. Enter a "
                        "relative output name; the isolated delivery worker creates it."
                    ),
                }
            )
        root = _owner_output_root()
        root.mkdir(parents=True, exist_ok=True)
        current = _safe_output_browser_dir(str(request.args.get("path", "")))
        if not current.exists() or not current.is_dir():
            raise ValueError("Selected output directory does not exist")
        relative = "" if current == root else current.relative_to(root).as_posix()
        directories = []
        for child in sorted(current.iterdir(), key=lambda item: item.name.casefold()):
            if not child.is_dir():
                continue
            resolved = child.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            directories.append(
                {
                    "name": child.name,
                    "path": child.relative_to(root).as_posix(),
                }
            )
        parent = None
        if current != root:
            parent_path = current.parent
            parent = "" if parent_path == root else parent_path.relative_to(root).as_posix()
        return jsonify(
            {
                "output_root": str(root),
                "path": relative,
                "parent": parent,
                "directories": directories,
            }
        )
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/output-directories")
def create_output_directory() -> Response:
    """Create one named output subdirectory below a safely resolved parent."""
    try:
        if STORAGE_LAYOUT.final_mount and STORAGE_LAYOUT.final_mount.is_linux_ntfs:
            return jsonify(
                {
                    "error": (
                        "Direct directory creation is disabled for Linux NTFS output; "
                        "enter the relative name and let the isolated delivery worker "
                        "create it after download validation"
                    )
                }
            ), 409
        body = request.get_json(silent=True) or {}
        parent = _safe_output_browser_dir(str(body.get("parent", "")))
        name = str(body.get("name", "")).strip()
        if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("Folder name must be one non-empty directory name")
        target = _safe_output_browser_dir(
            (Path(str(body.get("parent", ""))) / name).as_posix()
        )
        target.mkdir(parents=True, exist_ok=False)
        root = _owner_output_root()
        return jsonify(
            {
                "name": target.name,
                "path": target.relative_to(root).as_posix(),
            }
        ), 201
    except FileExistsError:
        return jsonify({"error": "A folder with that name already exists"}), 409
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/plan")
def plan_task() -> Response:
    try:
        params = _normalize_params(request.get_json(silent=True) or {})
        if params["workflow"] != "grid":
            raise ValueError("Use /api/plan/points for point sources")
        binding = _web_auth.bind_project(session, params["project"])
        return jsonify(
            _write_plan(params, _plan_grid(params), auth_binding=binding)
        )
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/aoi/inspect")
def inspect_aoi() -> Response:
    try:
        if request.mimetype != "multipart/form-data":
            raise ValueError("AOI inspection requires a multipart file upload")
        return jsonify(_inspect_aoi_upload())
    except (OSError, RuntimeError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/plan/points")
def plan_points() -> Response:
    plan_dir: Path | None = None
    try:
        body = _request_body()
        params = _normalize_params(body)
        if params["workflow"] != "points":
            raise ValueError("Point preflight requires workflow=point")
        if request.mimetype != "multipart/form-data":
            raise ValueError("Point preflight must use a multipart sample upload")
        binding = _web_auth.bind_project(session, params["project"])
        plan_dir = PLANS_DIR / uuid.uuid4().hex
        plan_dir.mkdir(parents=True)
        primary = _save_point_uploads(plan_dir)
        geometry_types = _inspect_point_geometry(primary, params["layer"] or None)
        source = PointSource.open(
            primary,
            layer=params["layer"] or None,
            geometry_mode=params["geometry_mode"],
            id_field=params["id_field"] or None,
            reference_grid=params["reference_grid"] or None,
            max_points=params["max_points"],
            read_batch_size=max(params["chunk_size"], 10_000),
        )
        report = preflight_point_source(
            source,
            params["years"],
            chunk_size=params["chunk_size"],
            audit_db=plan_dir / "point_preflight.sqlite",
        )
        if report.get("errors"):
            raise ValueError("; ".join(report["errors"]))
        params["samples_path"] = str(primary.resolve())
        params["sample_count"] = int(report["rows"])
        params["sample_sha256"] = report["sample_sha256"]
        raw_bytes = report["rows"] * len(params["years"]) * 64 * 4
        resources = plan_point_resources(
            chunk_size=params["chunk_size"],
            years=len(params["years"]),
            workers=params["workers"],
            snapshot=WEB_MEMORY_SNAPSHOT,
            global_request_limit=GLOBAL_REQUEST_LIMIT,
        )
        params["resolved_workers"] = resources.workers
        point_plan = {
            **report,
            "workflow": "points",
            "raw_bytes": raw_bytes,
            "raw_gib": raw_bytes / 1024**3,
            "source_name": primary.name,
            "source_crs": _point_source_crs(primary, params["layer"] or None),
            "source_geometry_types": geometry_types or ["coordinate_table"],
            "normalized_crs": "EPSG:4326",
            "resolution_m": 10.0,
            "resource_plan": resources.as_dict(),
            "memory": WEB_MEMORY_SNAPSHOT.as_dict(),
        }
        return jsonify(
            _write_plan(params, point_plan, plan_dir, auth_binding=binding)
        )
    except PermissionError as exc:
        if plan_dir is not None and plan_dir.exists():
            shutil.rmtree(plan_dir)
        return jsonify({"error": str(exc)}), 403
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if plan_dir is not None and plan_dir.exists():
            shutil.rmtree(plan_dir)
        return jsonify({"error": str(exc)}), 400


@app.get("/api/tasks")
def list_tasks() -> Response:
    with _task_lock:
        items = [
            _public_task(rid, task)
            for rid, task in sorted(
                _tasks.items(), key=lambda item: item[1].get("started_at", ""), reverse=True
            )
            if not task.get("hidden") and _owns(task)
        ]
    return jsonify(items)


@app.post("/api/tasks")
def create_task() -> Response:
    run_dir: Path | None = None
    slot_acquired = False
    submitted = False
    try:
        body = _request_body()
        token = str(body.get("planId") or body.get("plan_id") or "")
        confirmation = str(body.get("confirmation") or "")
        if not _submission_slots.acquire(blocking=False):
            return jsonify(
                {
                    "error": "Task queue is full",
                    "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
                    "max_queued_tasks": MAX_QUEUED_TASKS,
                }
            ), 429
        slot_acquired = True
        rid = run_id()
        with _task_lock:
            plan_dir, record = _read_plan(token, confirmation)
            _consume_plan(plan_dir, record, rid)
        params = record["params"]
        plan = record["plan"]
        auth_binding = record["auth_binding"]
        params["auth_source"] = auth_binding["auth_source"]
        output_dir = _safe_output_dir(params["output_name"], rid)
        with _task_lock:
            collision = any(
                Path(os.path.abspath(task.get("output_dir", ""))) == output_dir
                and task.get("status") in {"queued", "running", "cancelling", "validating_output"}
                for task in _tasks.values()
            )
        if collision:
            raise ValueError(f"Another active task is already writing to {output_dir}")
        run_dir = RUNS_DIR / rid
        run_dir.mkdir(parents=True)
        if params["workflow"] == "points":
            source_input = plan_dir / "input"
            target_input = run_dir / "input"
            if not source_input.exists():
                raise ValueError("Validated point upload is missing")
            shutil.move(str(source_input), str(target_input))
            params["samples_path"] = str(target_input / Path(params["samples_path"]).name)
        download_state_dir = run_dir / "download_state"
        download_state_dir.mkdir(parents=True)
        task_staging_dir = STAGING_ROOT / rid if STAGING_ROOT is not None else None
        if task_staging_dir is None:
            output_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "run.log"
        task = {
            "status": "queued",
            "progress": 0.0,
            "started_at": _now(),
            "finished_at": "",
            "params": params,
            "plan": plan,
            "log_path": log_path,
            "output_dir": output_dir,
            "download_state_dir": download_state_dir,
            "staging_dir": task_staging_dir,
            "cancel_event": threading.Event(),
            "grid_progress": {},
            "plan_id": plan_dir.name,
            "provenance": _provenance(),
            "owner_hash": record["owner_hash"],
            "auth_binding": auth_binding,
        }
        with _task_lock:
            _tasks[rid] = task
            _persist_task(rid)
        cmd = _build_cmd(
            params,
            output_dir,
            state_dir=download_state_dir,
            staging_dir=task_staging_dir,
        )
        with _task_lock:
            task["command"] = cmd
            _persist_task(rid)
        _executor.submit(_run_subprocess, rid, cmd, log_path)
        submitted = True
        return jsonify({"run_id": rid, "status": "queued", "plan": plan}), 202
    except PermissionError as exc:
        if run_dir is not None and run_dir.exists() and run_dir.parent == RUNS_DIR:
            shutil.rmtree(run_dir)
        return jsonify({"error": str(exc)}), 403
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if run_dir is not None and run_dir.exists() and run_dir.parent == RUNS_DIR:
            shutil.rmtree(run_dir)
        return jsonify({"error": str(exc)}), 400
    finally:
        if slot_acquired and not submitted:
            _submission_slots.release()


@app.get("/api/tasks/<rid>")
def get_task(rid: str) -> Response:
    with _task_lock:
        task = _tasks.get(rid)
        if task is None or task.get("hidden") or not _owns(task):
            return jsonify({"error": "Task not found"}), 404
        payload = {
            **_public_task(rid, task),
            "params": task.get("params", {}),
            "plan": task.get("plan"),
            "validation": task.get("validation"),
            "provenance": task.get("provenance"),
            "command": task.get("command"),
        }
    return jsonify(payload)


@app.get("/api/tasks/<rid>/results")
def task_results(rid: str) -> Response:
    with _task_lock:
        task = _tasks.get(rid)
        if task is None or task.get("hidden") or not _owns(task):
            return jsonify({"error": "Task not found"}), 404
        if not task.get("validation"):
            return jsonify({"error": "Validated results are not available yet"}), 409
        payload = {
            "run_id": rid,
            "status": task.get("status"),
            "validation": task["validation"],
            "provenance": task.get("provenance"),
            "output_dir": str(task.get("output_dir", "")),
        }
    return jsonify(payload)


@app.post("/api/tasks/<rid>/resume")
def resume_task(rid: str) -> Response:
    """Resume a checkpointed task after explicit reauthentication/project verification."""
    slot_acquired = False
    submitted = False
    try:
        if not _submission_slots.acquire(blocking=False):
            return jsonify({"error": "Task queue is full"}), 429
        slot_acquired = True
        body = request.get_json(silent=True) or {}
        with _task_lock:
            task = _tasks.get(rid)
            if task is None or task.get("hidden") or not _owns(task):
                return jsonify({"error": "Task not found"}), 404
            if task.get("status") not in {"auth_required", "interrupted", "failed", "error"}:
                return jsonify({"error": "Only stopped checkpointed tasks can be resumed"}), 409
            project = str(body.get("project") or task["params"].get("project") or "").strip()
        binding = _web_auth.bind_project(session, project)
        with _task_lock:
            task = _tasks[rid]
            task["params"]["project"] = project
            task["params"]["auth_source"] = binding.auth_source
            task["auth_binding"] = binding.as_dict()
            task["status"] = "queued"
            task["stage"] = "waiting_resources"
            task["error"] = None
            task["auth_failure"] = False
            task["finished_at"] = ""
            task["cancel_event"] = threading.Event()
            task["command"] = _build_cmd(
                task["params"],
                Path(task["output_dir"]),
                state_dir=Path(task["download_state_dir"]),
                staging_dir=Path(task["staging_dir"]) if task.get("staging_dir") else None,
            )
            _persist_task(rid)
            cmd = list(task["command"])
            log_path = Path(task["log_path"])
        _executor.submit(_run_subprocess, rid, cmd, log_path)
        submitted = True
        return jsonify({"run_id": rid, "status": "queued"}), 202
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        if slot_acquired and not submitted:
            _submission_slots.release()


@app.get("/api/tasks/<rid>/logfile")
def log_file(rid: str) -> Response:
    with _task_lock:
        task = _tasks.get(rid)
        if task is None or task.get("hidden") or not _owns(task):
            return jsonify({"error": "Task not found"}), 404
        path = Path(task["log_path"])
    if not path.exists():
        return Response("", mimetype="text/plain")
    return send_from_directory(path.parent, path.name, mimetype="text/plain")


@app.get("/api/tasks/<rid>/log")
def stream_log(rid: str) -> Response:
    with _task_lock:
        if (
            rid not in _tasks
            or _tasks[rid].get("hidden")
            or not _owns(_tasks[rid])
        ):
            return jsonify({"error": "Task not found"}), 404
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0

    def generate() -> Iterator[str]:
        position = offset
        while True:
            with _task_lock:
                task = _tasks.get(rid)
                if task is None:
                    break
                status = task.get("status")
                path = Path(task["log_path"])
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(position)
                    for raw_line in stream:
                        position += len(raw_line)
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if line.startswith(EVENT_PREFIX):
                            try:
                                event_payload = json.loads(line[len(EVENT_PREFIX) :])
                            except json.JSONDecodeError:
                                event_payload = {"event": "malformed", "raw": line}
                            with _task_lock:
                                current = _tasks.get(rid, {})
                                event_payload["overall_progress"] = current.get("progress", 0.0)
                            yield f"id: {position}\nevent: progress\ndata: {json.dumps(event_payload)}\n\n"
                        else:
                            yield f"id: {position}\nevent: log\ndata: {json.dumps(line)}\n\n"
            if status in TERMINAL_STATUSES:
                yield f"event: eof\ndata: {json.dumps({'offset': position})}\n\n"
                break
            yield ": keepalive\n\n"
            time.sleep(0.75)

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.delete("/api/tasks/<rid>")
def cancel_or_hide_task(rid: str) -> Response:
    with _task_lock:
        task = _tasks.get(rid)
        if task is None or not _owns(task):
            return jsonify({"error": "Task not found"}), 404
        if task["status"] in {"queued", "running", "cancelling", "validating_output"}:
            task["cancel_event"].set()
            task["status"] = "cancelling"
            process = _processes.get(rid)
            if process is not None and process.poll() is None:
                pid = task.get("pid") or process.pid
                create_time = task.get("process_create_time")
                threading.Thread(
                    target=_finish_cancellation,
                    args=(rid, pid, create_time),
                    daemon=True,
                    name=f"aef-cancel-{rid}",
                ).start()
            else:
                task["status"] = "cancelled"
                task["finished_at"] = _now()
            _persist_task(rid)
            return jsonify({"ok": True, "status": task["status"]})
        task["hidden"] = True
        _persist_task(rid)
    return jsonify({"ok": True, "status": "hidden"})


def _finish_cancellation(rid: str, pid: int, create_time: float | None) -> None:
    stopped = _terminate_process_tree(pid, create_time)
    if not stopped:
        with _task_lock:
            task = _tasks.get(rid)
            if task is not None:
                task["error"] = (
                    "The process tree was not stopped. A member may be in "
                    "uninterruptible disk sleep; no repeated kill was attempted."
                )
                _persist_task(rid)


def _shutdown_processes() -> None:
    with _task_lock:
        identities = [
            (rid, task.get("pid"), task.get("process_create_time"))
            for rid, task in _tasks.items()
            if task.get("status") in {"running", "cancelling", "validating_output"}
            and task.get("pid")
        ]
    for _, pid, create_time in identities:
        _terminate_process_tree(pid, create_time, grace_seconds=2.0)


def _remove_server_pid() -> None:
    try:
        if SERVER_PID_PATH.exists() and SERVER_PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            SERVER_PID_PATH.unlink()
    except OSError:
        pass


atexit.register(_shutdown_processes)
atexit.register(_remove_server_pid)


if __name__ == "__main__":
    SERVER_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    print("AEF-GRiTS UI starting on http://127.0.0.1:5555")
    app.run(host="127.0.0.1", port=5555, debug=False, threaded=True)
