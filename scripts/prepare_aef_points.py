#!/usr/bin/env python
"""Convert tabular or vector samples into the canonical AEF point table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet  # noqa: E402
from aef_grits.points import (  # noqa: E402
    load_point_source,
    raise_for_invalid_points,
    validate_point_table,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs" / "prepared_points" / "aef_points.parquet",
    )
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
        help="GeoTIFF or AEF Zarr defining exact centres for interior_pixels",
    )
    parser.add_argument("--max-points", type=int, default=1_000_000)
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2017, 2026)))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    points = load_point_source(
        args.input,
        layer=args.layer,
        geometry_mode=args.geometry_mode,
        id_field=args.id_field,
        reference_grid=args.reference_grid,
        max_points=args.max_points,
    )
    points, report = validate_point_table(points, args.years)
    report.update(
        {
            "source": str(args.input.resolve()),
            "output": str(args.output.resolve()),
            "geometry_mode": args.geometry_mode,
            "reference_grid": (
                str(args.reference_grid.resolve()) if args.reference_grid else None
            ),
        }
    )
    report_path = args.output.with_suffix(".validation.json")
    write_json(report, report_path)
    raise_for_invalid_points(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.suffix.lower() == ".csv":
        temporary = args.output.with_name(f".{args.output.name}.tmp")
        points.to_csv(temporary, index=False)
        temporary.replace(args.output)
    elif args.output.suffix.lower() == ".parquet":
        write_parquet(points, args.output)
    else:
        raise ValueError("--output must end in .csv or .parquet")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
