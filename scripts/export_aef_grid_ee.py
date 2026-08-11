#!/usr/bin/env python3
"""Submit annual AEF rasters on an exact user-provided reference grid.

The exporter keeps the 64 ``A00``-``A63`` float bands and uses the reference
GeoTIFF CRS, affine transform, bounds, width, and height.  This makes the
downloaded rasters directly compatible with the phase-2 nearest-neighbour
alignment code while avoiding an unnecessary 10 m export for a 30 m stage-1
map.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import ee
import rasterio


AEF_ASSET = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
AEF_BANDS = [f"A{i:02d}" for i in range(64)]
NODATA = -9999.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export annual 64-band AEF rasters on a reference grid."
    )
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--years", type=int, nargs="+", default=list(range(2017, 2026)))
    parser.add_argument("--folder", default="AEF_GRITS_RASTERS")
    parser.add_argument("--prefix", required=True)
    parser.add_argument(
        "--task-log",
        type=Path,
        default=Path("outputs/submitted_tasks/aef_raster_tasks.json"),
    )
    parser.add_argument("--priority", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="Submit even if an Earth Engine operation has the same description.",
    )
    return parser.parse_args()


def reference_grid(path: Path) -> dict:
    with rasterio.open(path) as src:
        transform = src.transform
        if src.crs is None:
            raise ValueError(f"reference raster has no CRS: {path}")
        if transform.b != 0 or transform.d != 0:
            raise ValueError("rotated/sheared reference grids are not supported")
        return {
            "path": str(path.resolve()),
            "crs": str(src.crs),
            "width": int(src.width),
            "height": int(src.height),
            "bounds": [float(value) for value in src.bounds],
            "crs_transform": [
                float(transform.a),
                float(transform.b),
                float(transform.c),
                float(transform.d),
                float(transform.e),
                float(transform.f),
            ],
            "pixels": int(src.width * src.height),
        }


def existing_descriptions() -> set[str]:
    operations = ee.data.listOperations()
    items = operations if isinstance(operations, list) else operations.get("operations", [])
    return {
        str(item.get("metadata", {}).get("description", ""))
        for item in items
        if item.get("metadata", {}).get("description")
    }


def annual_image(year: int, region: ee.Geometry) -> tuple[ee.Image, int]:
    collection = (
        ee.ImageCollection(AEF_ASSET)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .filterBounds(region)
    )
    count = int(collection.size().getInfo())
    if count == 0:
        raise ValueError(f"no AEF imagery intersects the reference grid for {year}")
    image = collection.mosaic().select(AEF_BANDS).toFloat()
    # Preserve a rectangular, fixed grid. Masked source pixels are encoded as
    # explicit nodata so rasterio/GDAL can validate all 64 bands consistently.
    image = image.unmask(NODATA, sameFootprint=False).clip(region)
    return image, count


def main() -> None:
    args = parse_args()
    years = sorted(set(args.years))
    if any(year < 2017 or year > 2025 for year in years):
        raise ValueError("this phase-2 protocol expects years within 2017-2025")
    if not 0 <= args.priority <= 9999:
        raise ValueError("priority must be between 0 and 9999")

    grid = reference_grid(args.reference)
    plan = {
        "asset": AEF_ASSET,
        "bands": AEF_BANDS,
        "dtype": "float32",
        "nodata": NODATA,
        "years": years,
        "drive_folder": args.folder,
        "prefix": args.prefix,
        "grid": grid,
        "estimated_uncompressed_gib_per_year": (
            grid["pixels"] * len(AEF_BANDS) * 4 / 2**30
        ),
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.dry_run:
        return

    ee.Initialize(project=args.project)
    xmin, ymin, xmax, ymax = grid["bounds"]
    region = ee.Geometry.Rectangle(
        [xmin, ymin, xmax, ymax], proj=grid["crs"], geodesic=False
    )
    known = existing_descriptions()
    submitted = []
    for year in years:
        description = f"{args.prefix}_{year}_64band_30m"
        if description in known and not args.allow_existing:
            raise RuntimeError(
                f"Earth Engine already has an operation named {description}; "
                "refusing a duplicate submission"
            )
        image, source_tiles = annual_image(year, region)
        task = ee.batch.Export.image.toDrive(
            image=image,
            description=description,
            folder=args.folder,
            fileNamePrefix=description,
            region=region,
            crs=grid["crs"],
            crsTransform=grid["crs_transform"],
            maxPixels=max(100_000_000, grid["pixels"] + 1),
            fileFormat="GeoTIFF",
            formatOptions={"cloudOptimized": True, "noData": NODATA},
            priority=args.priority,
        )
        task.start()
        record = {
            "year": year,
            "description": description,
            "task_id": task.id,
            "source_tiles": source_tiles,
        }
        submitted.append(record)
        print(
            f"[SUBMITTED] year={year} task_id={task.id} "
            f"source_tiles={source_tiles}"
        )

    payload = {**plan, "project": args.project, "tasks": submitted}
    args.task_log.parent.mkdir(parents=True, exist_ok=True)
    args.task_log.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Task log: {args.task_log}")


if __name__ == "__main__":
    main()
