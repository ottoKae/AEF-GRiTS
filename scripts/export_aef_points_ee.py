#!/usr/bin/env python
"""Submit one multi-year AlphaEarth table export per point chunk.

Combining annual bands into one image avoids serializing the same local point
FeatureCollection once per year.  Each exported CSV contains all requested
annual 64-D groups and can be consumed by ``merge_aef_point_exports.py``.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import ee
import pandas as pd

from aef_grits.earth_engine import initialize


DATASET = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
AEF_BANDS = [f"A{i:02d}" for i in range(64)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-csv", type=Path, required=True)
    parser.add_argument("--project")
    parser.add_argument(
        "--auth-source", choices=("auto", "earthengine", "adc", "web"), default=None
    )
    parser.add_argument("--folder", default="AEF_ALL_PLANTATION_POLYGONS")
    parser.add_argument("--prefix", default="plantation_polygon_pixels_2017_2025")
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2017, 2026)))
    parser.add_argument("--chunk-size", type=int, default=10_000)
    parser.add_argument("--tile-scale", type=int, default=8)
    parser.add_argument("--scale", type=int, default=10)
    parser.add_argument("--study-bounds", nargs=4, type=float, default=[-82, -6, -74, 3])
    parser.add_argument("--start-chunk", type=int, default=1)
    parser.add_argument("--max-chunks", type=int)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def annual_image(year: int, bounds: ee.Geometry) -> ee.Image:
    names = [f"aef{year}_center_{band}" for band in AEF_BANDS]
    return (
        ee.ImageCollection(DATASET)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .filterBounds(bounds)
        .select(AEF_BANDS)
        .mosaic()
        .rename(names)
    )


def build_features(frame: pd.DataFrame) -> ee.FeatureCollection:
    return ee.FeatureCollection(
        [
            ee.Feature(
                ee.Geometry.Point([float(row.lon), float(row.lat)], "EPSG:4326"),
                {"sample_id": str(row.sample_id)},
            )
            for row in frame.itertuples(index=False)
        ]
    )


def main() -> None:
    args = parse_args()
    if args.chunk_size <= 0:
        raise ValueError("--chunk-size must be positive")
    years = sorted(set(args.years))
    if not years:
        raise ValueError("at least one year is required")
    frame = pd.read_csv(args.sample_csv, usecols=["sample_id", "lon", "lat"])
    if frame.sample_id.duplicated().any() or frame[["lon", "lat"]].isna().any().any():
        raise ValueError("sample_id must be unique and coordinates complete")
    chunks = int(math.ceil(len(frame) / args.chunk_size))
    selected = list(range(max(1, args.start_chunk), chunks + 1))
    if args.max_chunks is not None:
        selected = selected[: args.max_chunks]
    plan = {
        "sample_csv": str(args.sample_csv.resolve()),
        "sample_count": len(frame),
        "years": years,
        "feature_columns": len(years) * 64,
        "chunk_size": args.chunk_size,
        "chunk_count_total": chunks,
        "chunk_count_selected": len(selected),
        "selected_chunks": selected,
        "project_configured": bool(args.project),
        "drive_folder": args.folder,
        "prefix": args.prefix,
        "study_bounds": args.study_bounds,
    }
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    if args.dry_run:
        return

    _, resolved_auth = initialize(
        args.project, auth_source=args.auth_source, return_auth=True
    )
    plan["authentication"] = resolved_auth.public_summary()
    bounds = ee.Geometry.Rectangle(args.study_bounds, None, False)
    image = ee.Image.cat([annual_image(year, bounds) for year in years])
    selectors = [
        "sample_id",
        *[f"aef{year}_center_{band}" for year in years for band in AEF_BANDS],
    ]
    submitted = []
    for chunk_id in selected:
        start = (chunk_id - 1) * args.chunk_size
        end = min(len(frame), chunk_id * args.chunk_size)
        samples = build_features(frame.iloc[start:end])
        extracted = image.sampleRegions(
            collection=samples,
            properties=["sample_id"],
            scale=args.scale,
            tileScale=args.tile_scale,
            geometries=False,
        )
        name = f"{args.prefix}_part{chunk_id:03d}"
        task = ee.batch.Export.table.toDrive(
            collection=extracted,
            description=name,
            folder=args.folder,
            fileNamePrefix=name,
            fileFormat="CSV",
            selectors=selectors,
        )
        task.start()
        info = {
            "chunk": chunk_id,
            "description": name,
            "task_id": task.id,
            "start_row": start,
            "end_row": end,
            "row_count": end - start,
        }
        submitted.append(info)
        print(f"[SUBMITTED] {name} rows={end-start} task_id={task.id}", flush=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"{args.prefix}_submitted_tasks.json"
    log_path.write_text(
        json.dumps({**plan, "submitted_tasks": submitted}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Task log: {log_path}")


if __name__ == "__main__":
    main()
