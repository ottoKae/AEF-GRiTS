#!/usr/bin/env python
"""Download bounded probes from one Tessera and one MGRS grid for validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np
from affine import Affine
from rasterio.transform import array_bounds
import zarr


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json  # noqa: E402
from aef_grits.earth_engine import initialize  # noqa: E402
from aef_grits.grids import GridSpec, MGRSGridProvider, Tessera01GridProvider  # noqa: E402
from scripts.stream_aef_grid_ee import _stream_one  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--out-dir", type=Path, default=ROOT / "outputs" / "tile_smoke"
    )
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--probe-size", type=int, default=256)
    parser.add_argument(
        "--tessera-tile",
        nargs=2,
        type=float,
        default=(-79.95, -1.05),
        metavar=("LON", "LAT"),
    )
    parser.add_argument(
        "--mgrs-index",
        type=Path,
        help="Optional override; defaults to the packaged global MGRS index",
    )
    parser.add_argument("--mgrs-tile", default="17MPU")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--high-volume", action="store_true")
    return parser.parse_args()


def centre_probe(grid: GridSpec, size: int) -> GridSpec:
    if size <= 0 or size > min(grid.width, grid.height):
        raise ValueError("probe-size must fit within both selected grids")
    row = (grid.height - size) // 2
    column = (grid.width - size) // 2
    transform = grid.transform * Affine.translation(column, row)
    bounds = tuple(float(value) for value in array_bounds(size, size, transform))
    return GridSpec(
        scheme=grid.scheme,
        grid_id=f"{grid.grid_id}__probe_{size}",
        crs=grid.crs,
        transform=transform,
        width=size,
        height=size,
        bounds=bounds,
        metadata={
            **grid.metadata,
            "parent_grid_id": grid.grid_id,
            "probe_row": row,
            "probe_column": column,
        },
    )


def main() -> None:
    args = parse_args()
    tessera = Tessera01GridProvider().get(
        args.tessera_tile[0], args.tessera_tile[1], coordinates_are_centres=True
    )
    mgrs = MGRSGridProvider(args.mgrs_index).get(args.mgrs_tile)
    probes = [centre_probe(tessera, args.probe_size), centre_probe(mgrs, args.probe_size)]
    stream_args = SimpleNamespace(
        block_size=args.probe_size,
        inner_chunk=64,
        shard_size=512,
        workers=args.workers,
        max_retries=args.max_retries,
        checkpoint_every=1,
        commit_timeout=3600,
        adopt_existing_complete=False,
        import_legacy_progress=None,
        keep_staging=False,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    state_dir = args.out_dir / ".state"
    staging_dir = args.out_dir / ".staging"
    catalog_path = state_dir / "catalog.parquet"
    ee = initialize(args.project, high_volume=args.high_volume)
    results = []
    for probe in probes:
        output = args.out_dir / probe.scheme / f"{probe.grid_id}.zarr"
        _stream_one(
            stream_args,
            [args.year],
            probe,
            output,
            catalog_path,
            state_dir,
            staging_dir,
            ee,
        )
        group = zarr.open_group(str(output), mode="r")
        values = np.asarray(group["embeddings"][0], dtype=np.float32)
        finite = np.isfinite(values)
        results.append(
            {
                "scheme": probe.scheme,
                "grid_id": probe.grid_id,
                "parent_grid_id": probe.metadata["parent_grid_id"],
                "shape": list(values.shape),
                "finite_values": int(finite.sum()),
                "finite_fraction": float(finite.mean()),
                "compression": {
                    "codec": group.attrs["aef:compression_codec"],
                    "level": group.attrs["aef:compression_level"],
                    "shuffle": group.attrs["aef:compression_shuffle"],
                },
                "zarr": str(output.resolve()),
            }
        )
    report = {
        "project": args.project,
        "year": args.year,
        "probe_size": args.probe_size,
        "passed": all(item["finite_values"] > 0 for item in results),
        "results": results,
    }
    write_json(report, args.out_dir / "smoke_report.json")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise RuntimeError("One or more tile probes contain no finite AEF values")


if __name__ == "__main__":
    main()
