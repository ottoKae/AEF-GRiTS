#!/usr/bin/env python
"""Inspect legacy Drive point plans; use stream_aef_points_ee for native sampling."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd


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


def main() -> None:
    args = parse_args()
    if not args.dry_run:
        raise ValueError(
            "Legacy Drive point sampling is retired because it does not preserve native pixels. "
            "Use: python -m scripts.stream_aef_points_ee --samples INPUT "
            "--years YEAR --out-dir NEW_DIRECTORY"
        )
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
        "status": "legacy_plan_only_not_native_verified",
        "replacement": "python -m scripts.stream_aef_points_ee",
    }
    print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
