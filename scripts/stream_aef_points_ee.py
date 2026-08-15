#!/usr/bin/env python
"""Stream annual AEF values from Earth Engine directly into Parquet shards.

This command uses the synchronous ``ee.data.computeFeatures`` endpoint. It does
not create Earth Engine batch tasks and does not use Google Drive. Each input
chunk is committed atomically and can be resumed independently.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet  # noqa: E402
from aef_grits.earth_engine import (  # noqa: E402
    AEF_RESOLUTION_M,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DATASET,
    EarthEngineRequestError,
    RequestPolicy,
    execute_with_retry,
    feature_columns,
    initialize,
    multiyear_image,
)
from aef_grits.points import (  # noqa: E402
    raise_for_invalid_points,
)
from aef_grits.point_source import (  # noqa: E402
    PointSource,
    point_run_signature,
    preflight_point_source,
)
from aef_grits.resource_budget import (  # noqa: E402
    DEFAULT_GLOBAL_REQUESTS,
    MemoryGuard,
    ResourceLimitError,
    memory_snapshot,
    plan_point_resources,
    resolve_resource_profile,
)
from aef_grits.resource_control import TokenPool, default_resource_root  # noqa: E402
from aef_grits.storage_safety import (  # noqa: E402
    commit_staged_tree,
    mount_for_path,
    validate_storage_layout,
)
from aef_grits.telemetry import RunTelemetry  # noqa: E402


EVENT_PREFIX = "AEF_EVENT "
DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get("AEF_GRITS_OUTPUT_ROOT", str(Path.cwd() / "outputs"))
).expanduser()


def emit_event(event: str, **payload) -> None:
    print(EVENT_PREFIX + json.dumps({"event": event, **payload}, separators=(",", ":")), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples",
        type=Path,
        required=True,
        help="CSV, Parquet, Shapefile, GeoPackage or GeoJSON sample source",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT / "point_stream",
        help="Output directory (default: $AEF_GRITS_OUTPUT_ROOT/point_stream or ./outputs/point_stream)",
    )
    parser.add_argument(
        "--project",
        help="Earth Engine quota project (required unless --validate-only)",
    )
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2017, 2026)))
    parser.add_argument("--chunk-size", type=int, default=1_000)
    parser.add_argument("--page-size", type=int, default=1_000)
    parser.add_argument("--scale", type=float, default=10.0)
    parser.add_argument("--tile-scale", type=int, default=8)
    parser.add_argument("--workers", default="auto", help="Positive integer or auto")
    parser.add_argument(
        "--resource-profile",
        choices=("workstation-auto", "low-memory-1g", "server-8g", "server-16g"),
        default="workstation-auto",
    )
    parser.add_argument("--memory-limit-gib")
    parser.add_argument("--memory-reserve-gib")
    parser.add_argument("--global-request-limit", type=int)
    parser.add_argument("--memory-high-watermark", type=float, default=0.80)
    parser.add_argument("--memory-critical-watermark", type=float, default=0.90)
    parser.add_argument("--resource-state-dir", type=Path)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Deadline for each Earth Engine API attempt (default: 300)",
    )
    parser.add_argument("--high-volume", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--layer", help="GeoPackage layer name")
    parser.add_argument("--id-field", help="Source field used to build sample_id")
    parser.add_argument(
        "--geometry-mode",
        choices=("auto", "representative", "centroid", "interior_pixels"),
        default="auto",
    )
    parser.add_argument(
        "--reference-grid",
        type=Path,
        help="GeoTIFF or AEF Zarr defining centres for interior_pixels",
    )
    parser.add_argument("--max-points", type=int, default=1_000_000)
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Native-filesystem root for validation, run state and reports",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        help="Native-filesystem root used before immutable final delivery",
    )
    parser.add_argument("--commit-timeout", type=float, default=21_600.0)
    parser.add_argument("--keep-staging", action="store_true")
    return parser.parse_args()


def _bounds(ee, frame: pd.DataFrame):
    epsilon = 1e-6
    return ee.Geometry.Rectangle(
        [
            float(frame.lon.min()) - epsilon,
            float(frame.lat.min()) - epsilon,
            float(frame.lon.max()) + epsilon,
            float(frame.lat.max()) + epsilon,
        ],
        None,
        False,
    )


def _request_chunk(
    ee,
    frame: pd.DataFrame,
    years: list[int],
    *,
    scale: float,
    tile_scale: int,
    page_size: int,
    max_retries: int,
    request_pool,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    telemetry: RunTelemetry | None = None,
) -> pd.DataFrame:
    bounds = _bounds(ee, frame)
    image = multiyear_image(years, bounds)
    features = ee.FeatureCollection(
        [
            ee.Feature(
                ee.Geometry.Point([float(row.lon), float(row.lat)], "EPSG:4326"),
                {"sample_id": str(row.sample_id)},
            )
            for row in frame.itertuples(index=False)
        ]
    )
    sampled = image.sampleRegions(
        collection=features,
        properties=["sample_id"],
        scale=scale,
        tileScale=tile_scale,
        geometries=False,
    )
    def request():
        token_context = telemetry.request_token(request_pool) if telemetry else request_pool.token()
        with token_context:
            return ee.data.computeFeatures(
                {
                    "expression": sampled,
                    "fileFormat": "PANDAS_DATAFRAME",
                    "pageSize": page_size,
                    "workloadTag": "aef_grits_points",
                }
            )

    return execute_with_retry(
        "computeFeatures",
        request,
        RequestPolicy(
            timeout_seconds=request_timeout_seconds,
            max_retries=max_retries,
        ),
        event=lambda name, payload: (
            telemetry.request_event(name, payload) if telemetry else None,
            emit_event(name, workflow="points", **payload),
        ),
    )


def _complete_chunk(
    ee,
    chunk_id: int,
    frame: pd.DataFrame,
    years: list[int],
    columns: list[str],
    args: argparse.Namespace,
    shard_path: Path,
    request_pool,
    telemetry: RunTelemetry | None = None,
) -> dict:
    if shard_path.exists() and not args.overwrite:
        existing = pd.read_parquet(shard_path)
        expected_ids = frame.sample_id.astype(str).tolist()
        if existing.sample_id.astype(str).tolist() != expected_ids:
            raise ValueError(f"Resume sample mismatch in {shard_path}")
        missing = [name for name in columns if name not in existing.columns]
        if missing:
            raise ValueError(f"Resume schema mismatch in {shard_path}: {missing[:3]}")
        complete = int(existing[columns].notna().all(axis=1).sum())
        return {
            "chunk": chunk_id,
            "path": str(shard_path.resolve()),
            "rows": len(existing),
            "complete_rows": complete,
            "status": "reused",
        }

    remote = _request_chunk(
        ee,
        frame,
        years,
        scale=args.scale,
        tile_scale=args.tile_scale,
        page_size=args.page_size,
        max_retries=args.max_retries,
        request_pool=request_pool,
        request_timeout_seconds=args.request_timeout_seconds,
        telemetry=telemetry,
    )
    if telemetry is not None:
        telemetry.add_received_bytes(int(remote.memory_usage(index=True, deep=True).sum()))
    if "sample_id" not in remote:
        raise ValueError("Earth Engine response has no sample_id")
    remote["sample_id"] = remote.sample_id.astype(str)
    if remote.sample_id.duplicated().any():
        raise ValueError("Earth Engine returned duplicate sample_id values")
    missing_columns = [name for name in columns if name not in remote.columns]
    if missing_columns:
        raise ValueError(
            f"Earth Engine response lacks {len(missing_columns)} AEF columns; "
            f"examples={missing_columns[:3]}"
        )
    base = frame.copy()
    base["sample_id"] = base.sample_id.astype(str)
    overlap = set(columns).intersection(base.columns)
    if overlap:
        raise ValueError(f"Input table already contains AEF columns: {sorted(overlap)[:3]}")
    result = base.merge(remote[["sample_id", *columns]], on="sample_id", how="left", validate="one_to_one")
    for year in years:
        annual = [name for name in columns if name.startswith(f"aef{year}_")]
        result[f"aef{year}_complete"] = result[annual].notna().all(axis=1)
    result["aef_complete_all_years"] = result[columns].notna().all(axis=1)
    if telemetry is None:
        write_parquet(result, shard_path)
    else:
        with telemetry.measure_write():
            write_parquet(result, shard_path)
    return {
        "chunk": chunk_id,
        "path": str(shard_path.resolve()),
        "rows": len(result),
        "complete_rows": int(result.aef_complete_all_years.sum()),
        "status": "downloaded",
    }


def main() -> None:
    args = parse_args()
    profile = resolve_resource_profile(
        args.resource_profile,
        memory_limit_gib=args.memory_limit_gib,
        memory_reserve_gib=args.memory_reserve_gib,
        global_request_limit=args.global_request_limit,
    )
    args.memory_limit_gib = profile["memory_limit_gib"]
    args.memory_reserve_gib = profile["memory_reserve_gib"]
    args.global_request_limit = profile["global_request_limit"]
    if args.page_size <= 0 or args.max_retries < 0:
        raise ValueError("page-size must be positive and max-retries non-negative")
    if args.request_timeout_seconds <= 0:
        raise ValueError("request-timeout-seconds must be positive")
    if args.commit_timeout <= 0:
        raise ValueError("commit-timeout must be positive")
    if args.global_request_limit <= 0:
        raise ValueError("global-request-limit must be positive")
    if not math.isclose(args.scale, AEF_RESOLUTION_M):
        raise ValueError(
            f"AEF point sampling is fixed at {AEF_RESOLUTION_M:g} m; "
            f"received --scale {args.scale:g}"
        )
    years = sorted(set(args.years))
    final_out = args.out_dir
    state_root = args.state_dir or final_out / ".aef_state"
    layout = validate_storage_layout(final_out, state_root, args.staging_dir)
    state_root = Path(layout.state_root)
    staging_root = Path(layout.staging_root) if layout.staging_root else None
    source = PointSource.open(
        args.samples,
        layer=args.layer,
        geometry_mode=args.geometry_mode,
        id_field=args.id_field,
        reference_grid=args.reference_grid,
        max_points=args.max_points,
        read_batch_size=max(args.chunk_size, 10_000),
    )
    validation = preflight_point_source(
        source,
        years,
        chunk_size=args.chunk_size,
        audit_db=state_root / "point_preflight.sqlite",
    )
    snapshot = memory_snapshot(
        limit_gib=args.memory_limit_gib,
        reserve_gib=args.memory_reserve_gib,
    )
    resource_plan = plan_point_resources(
        chunk_size=args.chunk_size,
        years=len(years),
        workers=args.workers,
        snapshot=snapshot,
        global_request_limit=args.global_request_limit,
        high_watermark=args.memory_high_watermark,
        critical_watermark=args.memory_critical_watermark,
    )
    args.workers = resource_plan.workers
    memory_guard = MemoryGuard(resource_plan)
    telemetry = RunTelemetry()
    resource_root = args.resource_state_dir or default_resource_root(state_root)
    resource_root = Path(resource_root).expanduser().absolute()
    resource_mount = mount_for_path(resource_root)
    if resource_mount and resource_mount.is_linux_ntfs:
        raise ValueError("Shared resource state must be on a native filesystem")
    if layout.final_mount and layout.final_mount.is_linux_ntfs and resource_mount is None:
        raise ValueError("Cannot verify the shared resource-state filesystem for NTFS output")
    resource_root.mkdir(parents=True, exist_ok=True)
    request_pool = TokenPool(
        resource_root / "earth_engine",
        resource_plan.global_request_limit,
        name="request",
    )
    emit_event(
        "preflight_complete",
        workflow="points",
        rows=validation["rows"],
        total_chunks=validation.get("estimated_shards"),
    )
    signature = point_run_signature(
        validation["sample_sha256"],
        years,
        scale=args.scale,
        tile_scale=args.tile_scale,
        chunk_size=args.chunk_size,
        dataset=DATASET,
        content_sha256=validation["content_sha256"],
    )
    work_out = (
        staging_root / f"points-{signature[:12]}" if staging_root is not None else final_out
    )
    work_out.mkdir(parents=True, exist_ok=True)
    validation.update(
        {
            "source": str(args.samples.resolve()),
            "output_directory": os.path.abspath(final_out),
            "working_directory": str(work_out.resolve()),
            "state_directory": str(state_root.resolve()),
            "geometry_mode": args.geometry_mode,
            "validate_only": bool(args.validate_only),
            "memory": snapshot.as_dict(),
            "resource_plan": resource_plan.as_dict(),
            "resource_state_dir": str(resource_root),
            "request_policy": RequestPolicy(
                timeout_seconds=args.request_timeout_seconds,
                max_retries=args.max_retries,
            ).as_dict(),
            "resource_profile": profile,
        }
    )
    write_json(validation, state_root / "validation_report.json")
    if work_out == final_out:
        write_json(validation, work_out / "validation_report.json")
    print(json.dumps(validation, indent=2, ensure_ascii=False))
    raise_for_invalid_points(validation)
    if args.validate_only:
        return
    if not args.project:
        raise ValueError("--project is required for an Earth Engine download")
    source.assert_unchanged(validation["source_fingerprint"])

    delivery_path = state_root / "delivery.json"
    if delivery_path.exists() and not args.overwrite:
        delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        if delivery.get("signature") != signature:
            raise ValueError("Existing point delivery has a different signature")
        if delivery.get("delivery_status") != "committed":
            delivery = None
    else:
        delivery = None
    if delivery is not None:
        report_path = state_root / "report.json"
        if not report_path.exists():
            raise FileNotFoundError("Completed delivery exists but its native report is missing")
        print(report_path.read_text(encoding="utf-8"))
        return
    shard_dir = work_out / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    columns = feature_columns(years)
    run_path = state_root / "run.json"
    if run_path.exists() and not args.overwrite:
        previous = json.loads(run_path.read_text(encoding="utf-8"))
        if previous.get("signature") != signature:
            raise ValueError("Existing output signature differs; use a new directory or --overwrite")
    write_json(
        {
            "signature": signature,
            "dataset": DATASET,
            "project": args.project,
            "years": years,
            "samples": str(args.samples.resolve()),
            "sample_count": validation["rows"],
            "chunk_size": args.chunk_size,
            "resolution_m": AEF_RESOLUTION_M,
            "feature_count": len(columns),
            "method": "ee.data.computeFeatures",
            "high_volume": args.high_volume,
            "request_policy": RequestPolicy(
                timeout_seconds=args.request_timeout_seconds,
                max_retries=args.max_retries,
            ).as_dict(),
            "resource_profile": profile,
        },
        run_path,
    )

    ee = initialize(
        args.project,
        high_volume=args.high_volume,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    started = time.perf_counter()
    records: list[dict] = []
    total_jobs = int(validation["estimated_shards"])
    chunk_iterator = iter(enumerate(source.iter_chunks(args.chunk_size), 1))
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        active = {}

        def submit_next() -> bool:
            pressure = memory_guard.sample()
            if pressure == "critical":
                raise ResourceLimitError("Critical memory watermark reached; completed shards are resumable")
            if pressure == "high" and active:
                return False
            try:
                chunk_id, chunk = next(chunk_iterator)
            except StopIteration:
                return False
            path = shard_dir / f"part{chunk_id:05d}.parquet"
            future = executor.submit(
                _complete_chunk,
                ee,
                chunk_id,
                chunk,
                years,
                columns,
                args,
                path,
                request_pool,
                telemetry,
            )
            active[future] = chunk_id
            return True

        for _ in range(resource_plan.max_in_flight):
            if not submit_next():
                break
        completed_count = 0
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                active.pop(future)
                record = future.result()
                records.append(record)
                completed_count += 1
                elapsed = time.perf_counter() - started
                print(
                    f"[{completed_count}/{total_jobs}] chunk={record['chunk']} "
                    f"rows={record['rows']} complete={record['complete_rows']} "
                    f"status={record['status']} elapsed={elapsed:.1f}s",
                    flush=True,
                )
                emit_event(
                    "progress",
                    workflow="points",
                    completed=completed_count,
                    total=total_jobs,
                    chunk=record["chunk"],
                    complete_rows=record["complete_rows"],
                    elapsed_seconds=elapsed,
                )
                submit_next()

    catalog = pd.DataFrame(sorted(records, key=lambda value: value["chunk"]))
    catalog["signature"] = signature
    catalog["years"] = json.dumps(years)
    catalog["feature_count"] = len(columns)
    catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    if work_out != final_out:
        catalog["path"] = catalog["path"].map(
            lambda value: os.path.abspath(final_out / "shards" / Path(value).name)
        )
    write_parquet(catalog, state_root / "catalog.parquet")
    if work_out == final_out:
        write_parquet(catalog, work_out / "catalog.parquet")
    report = {
        "signature": signature,
        "sample_count": validation["rows"],
        "shards": len(catalog),
        "complete_rows": int(catalog.complete_rows.sum()),
        "incomplete_rows": int(validation["rows"] - catalog.complete_rows.sum()),
        "elapsed_seconds": time.perf_counter() - started,
        "catalog": str((state_root / "catalog.parquet").resolve()),
        "final_output": os.path.abspath(final_out),
        "working_output": str(work_out.resolve()),
        "memory": snapshot.as_dict(),
        "resource_plan": resource_plan.as_dict(),
        **memory_guard.report(),
        "telemetry": telemetry.report(),
        "resource_profile": profile,
    }
    write_json(report, state_root / "report.json")
    if work_out == final_out:
        write_json(report, work_out / "report.json")
        write_json(
            {"signature": signature, "delivery_status": "committed", "final_output": str(final_out)},
            state_root / "delivery.json",
        )
    else:
        write_json(
            {"signature": signature, "workflow": "points", "complete_rows": report["complete_rows"]},
            work_out / "AEF_COMPLETE.json",
        )
        write_json(
            {
                "signature": signature,
                "delivery_status": "staging_complete",
                "working_output": str(work_out),
                "final_output": str(final_out),
            },
            state_root / "delivery.json",
        )
        validate_storage_layout(final_out, state_root, staging_root)
        with telemetry.measure_delivery():
            commit_staged_tree(
                work_out,
                final_out,
                signature=signature,
                state_dir=state_root,
                timeout_seconds=args.commit_timeout,
                resource_root=resource_root,
            )
        write_json(
            {
                "signature": signature,
                "delivery_status": "committed",
                "working_output": str(work_out),
                "final_output": str(final_out),
            },
            state_root / "delivery.json",
        )
        if not args.keep_staging:
            shutil.rmtree(work_out)
    report["elapsed_seconds"] = time.perf_counter() - started
    report["telemetry"] = telemetry.report()
    write_json(report, state_root / "report.json")
    if work_out == final_out:
        write_json(report, work_out / "report.json")
    print(json.dumps(report, indent=2))
    emit_event("run_complete", workflow="points", completed=len(catalog), total=total_jobs)


if __name__ == "__main__":
    try:
        main()
    except EarthEngineRequestError as exc:
        emit_event(
            "run_resumable_failure" if exc.resumable else "run_failed",
            workflow="points",
            operation=exc.operation,
            classification=exc.classification,
            attempts=exc.attempts,
            error=str(exc),
        )
        raise SystemExit(75 if exc.resumable else 1) from exc
