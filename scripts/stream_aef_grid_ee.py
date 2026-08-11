#!/usr/bin/env python
"""Stream annual AEF grid blocks from Earth Engine directly into local Zarr.

The command uses ``ee.data.computePixels`` and never stages GeoTIFFs in Google
Drive. Requests are bounded below Earth Engine's 48 MB uncompressed response
limit, while the destination is a resumable Zarr v3 embedding cube.
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import Affine
import zarr
from zarr.codecs import BloscCodec


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet  # noqa: E402
from aef_grits.earth_engine import AEF_BANDS, DATASET, annual_image, initialize  # noqa: E402


NODATA = -9999.0
BYTES_PER_VALUE = 4
COMPUTE_PIXELS_LIMIT = 48_000_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tile-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2017, 2026)))
    parser.add_argument("--block-size", type=int, default=256)
    parser.add_argument("--inner-chunk", type=int, default=32)
    parser.add_argument("--shard-size", type=int, default=512)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--checkpoint-every", type=int, default=8)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--high-volume", action="store_true")
    return parser.parse_args()


def reference_grid(path: Path) -> dict:
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError(f"Reference has no CRS: {path}")
        transform = source.transform
        if not math.isclose(transform.b, 0.0) or not math.isclose(transform.d, 0.0):
            raise ValueError("Rotated or sheared reference grids are not supported")
        return {
            "crs": str(source.crs),
            "transform": transform,
            "width": int(source.width),
            "height": int(source.height),
            "bounds": tuple(float(value) for value in source.bounds),
        }


def _signature(
    grid: dict,
    years: list[int],
    tile_id: str,
    *,
    block_size: int,
    inner_chunk: int,
    shard_size: int,
) -> str:
    payload = {
        "dataset": DATASET,
        "tile_id": tile_id,
        "years": years,
        "crs": grid["crs"],
        "transform": list(grid["transform"])[:6],
        "width": grid["width"],
        "height": grid["height"],
        "block_size": block_size,
        "inner_chunk": inner_chunk,
        "shard_size": shard_size,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _block_specs(grid: dict, years: list[int], block_size: int):
    for time_index, year in enumerate(years):
        for row in range(0, grid["height"], block_size):
            height = min(block_size, grid["height"] - row)
            for column in range(0, grid["width"], block_size):
                width = min(block_size, grid["width"] - column)
                yield {
                    "time_index": time_index,
                    "year": year,
                    "row": row,
                    "column": column,
                    "height": height,
                    "width": width,
                    "key": f"{year}:{row}:{column}",
                }


def _pixel_grid(grid: dict, block: dict) -> dict:
    transform: Affine = grid["transform"]
    return {
        "dimensions": {"width": block["width"], "height": block["height"]},
        "affineTransform": {
            "scaleX": float(transform.a),
            "shearX": float(transform.b),
            "translateX": float(transform.c + block["column"] * transform.a),
            "shearY": float(transform.d),
            "scaleY": float(transform.e),
            "translateY": float(transform.f + block["row"] * transform.e),
        },
        "crsCode": grid["crs"],
    }


def _fetch_block(ee, image, grid: dict, block: dict, max_retries: int):
    request = {
        "expression": image,
        "fileFormat": "NUMPY_NDARRAY",
        "bandIds": list(AEF_BANDS),
        "grid": _pixel_grid(grid, block),
        "workloadTag": "aef_grits_grid",
    }
    error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = ee.data.computePixels(request)
            values = np.stack(
                [np.asarray(response[band], dtype=np.float32) for band in AEF_BANDS]
            )
            values[values == NODATA] = np.nan
            return block, values
        except Exception as exc:
            error = exc
            if attempt >= max_retries:
                break
            time.sleep(min(60.0, 2.0**attempt))
    raise RuntimeError(f"Earth Engine grid request failed for {block['key']}: {error}")


def _create_store(
    path: Path,
    grid: dict,
    years: list[int],
    signature: str,
    inner_chunk: int,
    shard_size: int,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        group = zarr.open_group(str(path), mode="r+", zarr_format=3, use_consolidated=False)
        if group.attrs.get("aef:signature") != signature:
            raise ValueError("Existing Zarr signature differs; select a new output path")
        expected = (len(years), len(AEF_BANDS), grid["height"], grid["width"])
        if tuple(group["embeddings"].shape) != expected:
            raise ValueError("Existing Zarr array shape differs from the requested grid")
        return group

    group = zarr.open_group(str(path), mode="w", zarr_format=3)
    compressor = BloscCodec(cname="zstd", clevel=3)
    group.create_array(
        "embeddings",
        shape=(len(years), len(AEF_BANDS), grid["height"], grid["width"]),
        chunks=(1, len(AEF_BANDS), inner_chunk, inner_chunk),
        shards=(1, len(AEF_BANDS), shard_size, shard_size),
        dtype=np.float32,
        fill_value=np.nan,
        compressors=compressor,
        dimension_names=["time", "band", "y", "x"],
    )
    transform: Affine = grid["transform"]
    coordinates = {
        "time": np.asarray(years, dtype=np.int32),
        "band": np.arange(len(AEF_BANDS), dtype=np.int16),
        "x": transform.c + (np.arange(grid["width"]) + 0.5) * transform.a,
        "y": transform.f + (np.arange(grid["height"]) + 0.5) * transform.e,
    }
    for name, values in coordinates.items():
        array = group.create_array(
            name,
            data=values,
            compressors=compressor,
            dimension_names=[name],
        )
        array.attrs["_ARRAY_DIMENSIONS"] = [name]
    group["embeddings"].attrs.update(
        {
            "_ARRAY_DIMENSIONS": ["time", "band", "y", "x"],
            "long_name": "AlphaEarth Foundation annual embedding",
            "band_names": list(AEF_BANDS),
        }
    )
    group.attrs.update(
        {
            "aef:signature": signature,
            "aef:asset": DATASET,
            "aef:download_method": "ee.data.computePixels",
            "geoemb:model": "AlphaEarth Foundations",
            "geoemb:dimensions": len(AEF_BANDS),
            "tile_id": grid.get("tile_id"),
            "crs": grid["crs"],
            "transform": list(transform)[:6],
            "width": grid["width"],
            "height": grid["height"],
            "years": years,
            "bands": list(AEF_BANDS),
        }
    )
    return group


def _save_catalog(path: Path, record: dict) -> None:
    if path.exists():
        catalog = pd.read_parquet(path)
        if "tile_id" in catalog:
            catalog = catalog[catalog.tile_id.astype(str) != str(record["tile_id"])]
        catalog = pd.concat([catalog, pd.DataFrame([record])], ignore_index=True)
    else:
        catalog = pd.DataFrame([record])
    write_parquet(catalog, path)


def main() -> None:
    args = parse_args()
    years = sorted(set(args.years))
    if not years:
        raise ValueError("At least one year is required")
    for name in ("block_size", "inner_chunk", "shard_size", "workers", "checkpoint_every"):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if args.shard_size % args.inner_chunk:
        raise ValueError("shard-size must be divisible by inner-chunk")
    request_bytes = args.block_size**2 * len(AEF_BANDS) * BYTES_PER_VALUE
    if request_bytes > COMPUTE_PIXELS_LIMIT:
        raise ValueError(
            f"block-size={args.block_size} requests {request_bytes / 1e6:.1f} MB; "
            "Earth Engine computePixels allows at most 48 MB uncompressed"
        )

    grid = reference_grid(args.reference)
    grid["tile_id"] = args.tile_id
    signature = _signature(
        grid,
        years,
        args.tile_id,
        block_size=args.block_size,
        inner_chunk=args.inner_chunk,
        shard_size=args.shard_size,
    )
    progress_path = args.out.with_name(f"{args.out.name}.progress.json")
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress.get("signature") != signature:
            raise ValueError("Progress signature differs; use a new output path")
    else:
        progress = {"signature": signature, "completed": []}
    completed = set(progress.get("completed", []))
    store = _create_store(
        args.out,
        grid,
        years,
        signature,
        args.inner_chunk,
        args.shard_size,
    )
    target = store["embeddings"]
    ee = initialize(args.project, high_volume=args.high_volume)
    xmin, ymin, xmax, ymax = grid["bounds"]
    bounds = ee.Geometry.Rectangle([xmin, ymin, xmax, ymax], proj=grid["crs"], geodesic=False)
    images = {
        year: annual_image(year, bounds).unmask(NODATA, sameFootprint=False)
        for year in years
    }
    all_blocks = list(_block_specs(grid, years, args.block_size))
    pending = [block for block in all_blocks if block["key"] not in completed]
    print(
        json.dumps(
            {
                "tile_id": args.tile_id,
                "shape": list(target.shape),
                "zarr_chunks": list(target.chunks),
                "zarr_shards": list(target.shards),
                "request_block": args.block_size,
                "request_uncompressed_mb": request_bytes / 1e6,
                "blocks_total": len(all_blocks),
                "blocks_complete": len(completed),
                "blocks_pending": len(pending),
            },
            indent=2,
        )
    )

    started = time.perf_counter()
    since_checkpoint = 0
    iterator = iter(pending)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        active = {}

        def submit_next() -> bool:
            try:
                block = next(iterator)
            except StopIteration:
                return False
            future = executor.submit(
                _fetch_block,
                ee,
                images[block["year"]],
                grid,
                block,
                args.max_retries,
            )
            active[future] = block
            return True

        for _ in range(max(1, args.workers * 2)):
            if not submit_next():
                break
        written = 0
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                active.pop(future)
                block, values = future.result()
                row, column = block["row"], block["column"]
                target[
                    block["time_index"],
                    :,
                    row : row + block["height"],
                    column : column + block["width"],
                ] = values
                completed.add(block["key"])
                written += 1
                since_checkpoint += 1
                if since_checkpoint >= args.checkpoint_every:
                    write_json(
                        {"signature": signature, "completed": sorted(completed)},
                        progress_path,
                    )
                    since_checkpoint = 0
                elapsed = time.perf_counter() - started
                rate = written / elapsed if elapsed else 0.0
                remaining = len(pending) - written
                eta = remaining / rate if rate else math.inf
                print(
                    f"[{len(completed)}/{len(all_blocks)}] {block['key']} "
                    f"valid={int(np.isfinite(values).all(axis=0).sum())} "
                    f"rate={rate:.2f} blocks/s eta={eta / 60:.1f} min",
                    flush=True,
                )
                submit_next()
    write_json({"signature": signature, "completed": sorted(completed)}, progress_path)
    if len(completed) != len(all_blocks):
        raise RuntimeError("Run ended before every block was committed")

    zarr.consolidate_metadata(str(args.out))
    catalog_path = args.catalog or args.out.parent / "catalog.parquet"
    record = {
        "tile_id": args.tile_id,
        "product_type": "aef_annual",
        "zarr_path": str(args.out.resolve()),
        "crs": grid["crs"],
        "transform": json.dumps(list(grid["transform"])[:6]),
        "width": grid["width"],
        "height": grid["height"],
        "years": json.dumps(years),
        "bands": json.dumps(list(AEF_BANDS)),
        "dtype": "float32",
        "status": "complete",
        "signature": signature,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_catalog(catalog_path, record)
    report = {
        **record,
        "blocks": len(all_blocks),
        "elapsed_seconds": time.perf_counter() - started,
        "catalog": str(catalog_path.resolve()),
        "progress": str(progress_path.resolve()),
    }
    write_json(report, args.out.with_name(f"{args.out.name}.report.json"))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
