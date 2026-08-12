#!/usr/bin/env python
"""Extract centred AEF patches from a local Zarr catalog into NPY shards."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet  # noqa: E402
from aef_grits.points import (  # noqa: E402
    load_point_source,
    raise_for_invalid_points,
    validate_point_table,
)
from aef_grits.store import AEFCatalog  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "outputs" / "patches",
        help="Output directory (default: <repository>/outputs/patches)",
    )
    parser.add_argument("--patch-size", type=int, default=9)
    parser.add_argument("--years", nargs="+", type=int)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--grid-id-column",
        help="Optional sample column containing catalog grid IDs",
    )
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--layer")
    parser.add_argument("--id-field")
    parser.add_argument(
        "--geometry-mode",
        choices=("auto", "representative", "centroid", "interior_pixels"),
        default="auto",
    )
    parser.add_argument("--reference-grid", type=Path)
    parser.add_argument("--max-points", type=int, default=1_000_000)
    return parser.parse_args()


def _point_signature(
    frame: pd.DataFrame,
    catalog_path: Path,
    years: list[int],
    patch_size: int,
    batch_size: int,
    grid_id_column: str | None,
) -> str:
    digest = hashlib.sha256()
    columns = ["sample_id", "lon", "lat"] + (
        [grid_id_column] if grid_id_column else []
    )
    for row in frame[columns].itertuples(index=False, name=None):
        digest.update(("\t".join(map(str, row)) + "\n").encode())
    catalog_digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    payload = {
        "samples_sha256": digest.hexdigest(),
        "catalog_sha256": catalog_digest,
        "years": years,
        "patch_size": patch_size,
        "batch_size": batch_size,
        "grid_id_column": grid_id_column,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _write_npy(values: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        np.save(stream, values, allow_pickle=False)
    os.replace(temporary, path)


def _auto_grid_column(frame: pd.DataFrame, requested: str | None) -> str | None:
    if requested:
        if requested not in frame:
            raise ValueError(f"Grid ID column is absent: {requested}")
        return requested
    for candidate in ("grid_id", "mgrs_tile", "tile_id"):
        if candidate in frame:
            return candidate
    return None


def main() -> None:
    args = parse_args()
    if args.patch_size <= 0 or args.patch_size % 2 != 1:
        raise ValueError("patch-size must be a positive odd integer")
    if args.batch_size <= 0:
        raise ValueError("batch-size must be positive")
    points = load_point_source(
        args.samples,
        layer=args.layer,
        geometry_mode=args.geometry_mode,
        id_field=args.id_field,
        reference_grid=args.reference_grid,
        max_points=args.max_points,
    )
    catalog = AEFCatalog(args.catalog)
    first = next(iter(catalog.stores.values()))
    years = first.years if args.years is None else sorted(set(args.years))
    points, validation = validate_point_table(points, years, chunk_size=args.batch_size)
    grid_column = _auto_grid_column(points, args.grid_id_column)
    if grid_column:
        missing_grid = int(points[grid_column].isna().sum())
        unknown = sorted(
            set(points[grid_column].dropna().astype(str)).difference(catalog.stores)
        )
        if missing_grid:
            validation["errors"].append(
                f"Rows with missing {grid_column}: {missing_grid}"
            )
        if unknown:
            validation["errors"].append(f"Unknown catalog grid IDs: {unknown[:20]}")
        validation["valid"] = not validation["errors"]
    unavailable = {
        grid_id: sorted(set(years).difference(store.years))
        for grid_id, store in catalog.stores.items()
        if set(years).difference(store.years)
    }
    if unavailable:
        validation["errors"].append(f"Requested years missing by grid: {unavailable}")
        validation["valid"] = False
    bytes_per_sample = len(years) * 64 * args.patch_size**2 * 4
    validation.update(
        {
            "catalog": str(args.catalog.resolve()),
            "catalog_grids": sorted(catalog.stores),
            "output_directory": str(args.out_dir.resolve()),
            "patch_size": args.patch_size,
            "patch_shape": [len(years), 64, args.patch_size, args.patch_size],
            "grid_id_column": grid_column,
            "bytes_per_sample_uncompressed": bytes_per_sample,
            "estimated_total_bytes_uncompressed": bytes_per_sample * len(points),
            "validate_only": bool(args.validate_only),
        }
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_json(validation, args.out_dir / "validation_report.json")
    print(json.dumps(validation, indent=2, ensure_ascii=False))
    raise_for_invalid_points(validation)
    if args.validate_only:
        return

    signature = _point_signature(
        points, args.catalog, years, args.patch_size, args.batch_size, grid_column
    )
    run_path = args.out_dir / "run.json"
    if run_path.exists() and not args.overwrite:
        previous = json.loads(run_path.read_text(encoding="utf-8"))
        if previous.get("signature") != signature:
            raise ValueError("Existing patch run differs; use a new directory or --overwrite")
    write_json(
        {
            "signature": signature,
            "samples": str(args.samples.resolve()),
            "catalog": str(args.catalog.resolve()),
            "years": years,
            "patch_size": args.patch_size,
            "batch_size": args.batch_size,
            "grid_id_column": grid_column,
            "shape": [len(points), len(years), 64, args.patch_size, args.patch_size],
        },
        run_path,
    )

    shard_dir = args.out_dir / "shards"
    metadata_dir = args.out_dir / "metadata"
    records = []
    for shard_id, start in enumerate(range(0, len(points), args.batch_size), 1):
        stop = min(len(points), start + args.batch_size)
        shard = points.iloc[start:stop].copy()
        value_path = shard_dir / f"part{shard_id:05d}.npy"
        metadata_path = metadata_dir / f"part{shard_id:05d}.parquet"
        expected_ids = shard.sample_id.astype(str).tolist()
        if value_path.exists() and metadata_path.exists() and not args.overwrite:
            metadata = pd.read_parquet(metadata_path)
            values = np.load(value_path, mmap_mode="r", allow_pickle=False)
            expected_shape = (
                len(shard), len(years), 64, args.patch_size, args.patch_size
            )
            if metadata.sample_id.astype(str).tolist() != expected_ids:
                raise ValueError(f"Resume sample mismatch: {metadata_path}")
            if values.shape != expected_shape or values.dtype != np.float32:
                raise ValueError(f"Resume tensor mismatch: {value_path}")
            status = "reused"
        else:
            coords = list(zip(shard.lon.astype(float), shard.lat.astype(float)))
            grid_ids = (
                shard[grid_column].astype(str).tolist() if grid_column else None
            )
            values = catalog.sample_patches(
                coords,
                patch_size=args.patch_size,
                years=years,
                crs="EPSG:4326",
                grid_ids=grid_ids,
            )
            finite = np.isfinite(values)
            metadata = shard.copy()
            metadata["patch_complete"] = finite.reshape(len(shard), -1).all(axis=1)
            metadata["patch_valid_fraction"] = finite.reshape(len(shard), -1).mean(axis=1)
            metadata["patch_shard"] = str(value_path.resolve())
            metadata["patch_index"] = np.arange(len(shard), dtype=np.int64)
            _write_npy(values, value_path)
            write_parquet(metadata, metadata_path)
            status = "downloaded"
        complete = int(metadata.patch_complete.sum())
        if args.require_complete and complete != len(metadata):
            raise ValueError(
                f"Incomplete patches in shard {shard_id}: {len(metadata) - complete}"
            )
        records.append(
            {
                "shard": shard_id,
                "value_path": str(value_path.resolve()),
                "metadata_path": str(metadata_path.resolve()),
                "rows": len(metadata),
                "complete_rows": complete,
                "status": status,
                "signature": signature,
            }
        )
        print(
            f"[{shard_id}/{math.ceil(len(points) / args.batch_size)}] "
            f"rows={len(metadata)} complete={complete} status={status}",
            flush=True,
        )
    output_catalog = pd.DataFrame(records)
    output_catalog["years"] = json.dumps(years)
    output_catalog["patch_size"] = args.patch_size
    output_catalog["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_parquet(output_catalog, args.out_dir / "catalog.parquet")
    report = {
        "signature": signature,
        "rows": len(points),
        "shards": len(records),
        "complete_rows": int(output_catalog.complete_rows.sum()),
        "incomplete_rows": int(len(points) - output_catalog.complete_rows.sum()),
        "shape": [len(points), len(years), 64, args.patch_size, args.patch_size],
        "catalog": str((args.out_dir / "catalog.parquet").resolve()),
    }
    write_json(report, args.out_dir / "report.json")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
