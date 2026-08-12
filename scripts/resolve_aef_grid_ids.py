#!/usr/bin/env python
"""Convert a vector AOI into MGRS and/or Tessera 0.1-degree grid ID lists."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet  # noqa: E402
from aef_grits.grid_lookup import (  # noqa: E402
    build_regions,
    build_search_catalog,
    read_aoi,
    resolve_mgrs,
    resolve_tessera,
)


def _default_mgrs_index() -> Path | None:
    configured = os.environ.get("AEF_GRITS_MGRS_INDEX")
    candidates = [
        Path(configured) if configured else None,
        ROOT / "data" / "mgrs.parquet",
        ROOT.parent / "S1-GRiTS" / "src" / "s1grits" / "data" / "mgrs.parquet",
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aoi", type=Path, required=True)
    parser.add_argument("--layer", help="GeoPackage layer name")
    parser.add_argument(
        "--schemes",
        nargs="+",
        choices=("mgrs", "tessera_0p1"),
        default=("mgrs", "tessera_0p1"),
    )
    parser.add_argument("--mgrs-index", type=Path, default=_default_mgrs_index())
    parser.add_argument("--region-id-field")
    parser.add_argument("--name-field-cn")
    parser.add_argument("--name-field-en")
    parser.add_argument("--default-region-id", default="aoi")
    parser.add_argument(
        "--include-touching",
        action="store_true",
        help="Include grids that only touch an AOI boundary",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "outputs" / "grid_lookup",
    )
    return parser.parse_args()


def _write_lines(values: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    args = parse_args()
    aoi = read_aoi(args.aoi, layer=args.layer)
    regions = build_regions(
        aoi,
        region_id_field=args.region_id_field,
        name_field_cn=args.name_field_cn,
        name_field_en=args.name_field_en,
        default_region_id=args.default_region_id,
    )

    rows: list[dict] = []
    if "mgrs" in args.schemes:
        if args.mgrs_index is None or not args.mgrs_index.is_file():
            raise FileNotFoundError(
                "MGRS mode requires --mgrs-index, AEF_GRITS_MGRS_INDEX, or "
                "the adjacent S1-GRiTS data/mgrs.parquet"
            )
        rows.extend(
            resolve_mgrs(
                regions,
                args.mgrs_index,
                include_touching=args.include_touching,
            )
        )
    if "tessera_0p1" in args.schemes:
        rows.extend(resolve_tessera(regions, include_touching=args.include_touching))

    intersections = pd.DataFrame.from_records(rows)
    if intersections.empty:
        raise ValueError("The AOI did not select any grid")
    intersections = intersections.sort_values(
        ["region_id", "scheme", "grid_id"]
    ).reset_index(drop=True)
    catalog = build_search_catalog(intersections)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_parquet(intersections, args.out_dir / "grid_intersections.parquet")
    write_parquet(catalog, args.out_dir / "grid_catalog.parquet")

    by_scheme: dict[str, list[str]] = {}
    for scheme in args.schemes:
        ids = sorted(
            set(intersections.loc[intersections.scheme == scheme, "grid_id"].astype(str))
        )
        by_scheme[scheme] = ids
        _write_lines(ids, args.out_dir / f"{scheme}_grid_ids.txt")

    by_region = {}
    for region_id, group in intersections.groupby("region_id", sort=True):
        by_region[str(region_id)] = {
            scheme: sorted(
                set(group.loc[group.scheme == scheme, "grid_id"].astype(str))
            )
            for scheme in args.schemes
        }
    write_json(
        {
            "source_aoi": str(args.aoi.resolve()),
            "source_crs": str(aoi.crs),
            "region_count": len(regions),
            "include_touching": bool(args.include_touching),
            "grid_ids": by_scheme,
            "regions": by_region,
            "outputs": {
                "intersections": "grid_intersections.parquet",
                "search_catalog": "grid_catalog.parquet",
            },
        },
        args.out_dir / "grid_ids.json",
    )

    counts = ", ".join(f"{scheme}={len(ids)}" for scheme, ids in by_scheme.items())
    print(f"Resolved {len(regions)} region(s): {counts}")
    print(f"Outputs: {args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
