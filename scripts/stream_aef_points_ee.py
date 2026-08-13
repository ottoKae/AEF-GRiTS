#!/usr/bin/env python
"""Stream annual AEF values from Earth Engine directly into Parquet shards.

This command uses the synchronous ``ee.data.computeFeatures`` endpoint. It does
not create Earth Engine batch tasks and does not use Google Drive. Each input
chunk is committed atomically and can be resumed independently.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
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
    DATASET,
    feature_columns,
    initialize,
    multiyear_image,
)
from aef_grits.points import (  # noqa: E402
    load_point_source,
    raise_for_invalid_points,
    validate_point_table,
)


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
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=6)
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
    return parser.parse_args()


def _signature(frame: pd.DataFrame, years: list[int], args: argparse.Namespace) -> str:
    sample_hash = hashlib.sha256()
    for row in frame[["sample_id", "lon", "lat"]].itertuples(index=False):
        sample_hash.update(f"{row.sample_id}\t{row.lon:.10f}\t{row.lat:.10f}\n".encode())
    payload = {
        "dataset": DATASET,
        "years": years,
        "scale": args.scale,
        "tile_scale": args.tile_scale,
        "chunk_size": args.chunk_size,
        "sample_sha256": sample_hash.hexdigest(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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
    error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return ee.data.computeFeatures(
                {
                    "expression": sampled,
                    "fileFormat": "PANDAS_DATAFRAME",
                    "pageSize": page_size,
                    "workloadTag": "aef_grits_points",
                }
            )
        except Exception as exc:  # Earth Engine exposes several transient classes.
            error = exc
            if attempt >= max_retries:
                break
            time.sleep(min(60.0, 2.0**attempt))
    raise RuntimeError(f"Earth Engine point request failed after retries: {error}")


def _complete_chunk(
    ee,
    chunk_id: int,
    frame: pd.DataFrame,
    years: list[int],
    columns: list[str],
    args: argparse.Namespace,
    shard_path: Path,
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
    )
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
    if args.page_size <= 0 or args.workers <= 0 or args.max_retries < 0:
        raise ValueError("page-size/workers must be positive and max-retries non-negative")
    if not math.isclose(args.scale, AEF_RESOLUTION_M):
        raise ValueError(
            f"AEF point sampling is fixed at {AEF_RESOLUTION_M:g} m; "
            f"received --scale {args.scale:g}"
        )
    years = sorted(set(args.years))
    frame = load_point_source(
        args.samples,
        layer=args.layer,
        geometry_mode=args.geometry_mode,
        id_field=args.id_field,
        reference_grid=args.reference_grid,
        max_points=args.max_points,
    )
    frame, validation = validate_point_table(frame, years, chunk_size=args.chunk_size)
    emit_event(
        "preflight_complete",
        workflow="points",
        rows=len(frame),
        total_chunks=validation.get("estimated_shards"),
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    validation.update(
        {
            "source": str(args.samples.resolve()),
            "output_directory": str(args.out_dir.resolve()),
            "geometry_mode": args.geometry_mode,
            "validate_only": bool(args.validate_only),
        }
    )
    write_json(validation, args.out_dir / "validation_report.json")
    print(json.dumps(validation, indent=2, ensure_ascii=False))
    raise_for_invalid_points(validation)
    if args.validate_only:
        return
    if not args.project:
        raise ValueError("--project is required for an Earth Engine download")

    shard_dir = args.out_dir / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    columns = feature_columns(years)
    signature = _signature(frame, years, args)
    run_path = args.out_dir / "run.json"
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
            "sample_count": len(frame),
            "chunk_size": args.chunk_size,
            "resolution_m": AEF_RESOLUTION_M,
            "feature_count": len(columns),
            "method": "ee.data.computeFeatures",
            "high_volume": args.high_volume,
        },
        run_path,
    )

    ee = initialize(args.project, high_volume=args.high_volume)
    jobs = []
    for chunk_id, start in enumerate(range(0, len(frame), args.chunk_size), 1):
        stop = min(len(frame), start + args.chunk_size)
        jobs.append(
            (
                chunk_id,
                frame.iloc[start:stop].copy(),
                shard_dir / f"part{chunk_id:05d}.parquet",
            )
        )

    started = time.perf_counter()
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _complete_chunk, ee, chunk_id, chunk, years, columns, args, path
            ): chunk_id
            for chunk_id, chunk, path in jobs
        }
        for completed, future in enumerate(as_completed(futures), 1):
            record = future.result()
            records.append(record)
            elapsed = time.perf_counter() - started
            print(
                f"[{completed}/{len(jobs)}] chunk={record['chunk']} "
                f"rows={record['rows']} complete={record['complete_rows']} "
                f"status={record['status']} elapsed={elapsed:.1f}s",
                flush=True,
            )
            emit_event(
                "progress",
                workflow="points",
                completed=completed,
                total=len(jobs),
                chunk=record["chunk"],
                complete_rows=record["complete_rows"],
                elapsed_seconds=elapsed,
            )

    catalog = pd.DataFrame(sorted(records, key=lambda value: value["chunk"]))
    catalog["signature"] = signature
    catalog["years"] = json.dumps(years)
    catalog["feature_count"] = len(columns)
    catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_parquet(catalog, args.out_dir / "catalog.parquet")
    report = {
        "signature": signature,
        "sample_count": len(frame),
        "shards": len(catalog),
        "complete_rows": int(catalog.complete_rows.sum()),
        "incomplete_rows": int(len(frame) - catalog.complete_rows.sum()),
        "elapsed_seconds": time.perf_counter() - started,
        "catalog": str((args.out_dir / "catalog.parquet").resolve()),
    }
    write_json(report, args.out_dir / "report.json")
    print(json.dumps(report, indent=2))
    emit_event("run_complete", workflow="points", completed=len(catalog), total=len(jobs))


if __name__ == "__main__":
    main()
