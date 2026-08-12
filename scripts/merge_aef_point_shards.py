#!/usr/bin/env python
"""Validate and merge a direct-stream point catalog into one Parquet table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet  # noqa: E402
from aef_grits.point_store import load_aef_points  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs" / "point_features" / "aef_points.parquet",
    )
    parser.add_argument("--years", nargs="+", type=int)
    parser.add_argument("--require-complete", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dataset = load_aef_points(
        args.catalog, years=args.years, require_complete=args.require_complete
    )
    write_parquet(dataset.frame, args.output)
    report = dict(dataset.report)
    report["output"] = str(args.output.resolve())
    write_json(report, args.output.with_suffix(".report.json"))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
