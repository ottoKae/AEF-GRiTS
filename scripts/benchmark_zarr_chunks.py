#!/usr/bin/env python
"""Benchmark AEF resolver reads for alternative lossless Zarr chunk sizes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np
from rasterio.transform import array_bounds
import zarr


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json  # noqa: E402
from aef_grits.store import AEFZarr  # noqa: E402
from scripts.stream_aef_grid_ee import _create_store  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Directory containing one source Zarr per tile",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "outputs" / "zarr_chunk_benchmark",
    )
    parser.add_argument("--chunks", nargs="+", type=int, default=[32, 64])
    parser.add_argument("--point-reads", type=int, default=128)
    parser.add_argument("--patch-reads", type=int, default=64)
    parser.add_argument("--block-reads", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _grid_from_store(store: AEFZarr) -> dict:
    return {
        "crs": store.crs,
        "transform": store.transform,
        "width": store.width,
        "height": store.height,
        "bounds": tuple(
            float(value)
            for value in array_bounds(store.height, store.width, store.transform)
        ),
        "tile_id": store.tile_id,
        "grid_id": store.grid_id,
        "scheme": store.scheme,
    }


def _build_store(
    source: AEFZarr, output: Path, inner_chunk: int, overwrite: bool
) -> dict:
    if output.exists() and not overwrite:
        candidate = AEFZarr(output)
        if (
            candidate.array.chunks[2:] != (inner_chunk, inner_chunk)
            or candidate.years != source.years
            or candidate.array.shape != source.array.shape
        ):
            raise ValueError(f"Existing benchmark store has wrong contract: {output}")
        started = time.perf_counter()
        exact = np.array_equal(
            np.asarray(candidate.array[:]), np.asarray(source.array[:]), equal_nan=True
        )
        return {
            "path": str(output.resolve()),
            "write_seconds": 0.0,
            "verification_seconds": time.perf_counter() - started,
            "bit_exact": bool(exact),
            "bytes": _directory_bytes(output),
            "status": "reused",
        }
    started = time.perf_counter()
    source_values = np.asarray(source.array[:], dtype=np.float32)
    group = _create_store(
        output,
        _grid_from_store(source),
        source.years,
        f"benchmark-chunk-{inner_chunk}",
        inner_chunk,
        512,
    )
    group["embeddings"][:] = source_values
    zarr.consolidate_metadata(str(output))
    write_seconds = time.perf_counter() - started
    started = time.perf_counter()
    decoded = np.asarray(zarr.open_group(str(output), mode="r")["embeddings"][:])
    exact = np.array_equal(source_values, decoded, equal_nan=True)
    verification_seconds = time.perf_counter() - started
    return {
        "path": str(output.resolve()),
        "write_seconds": write_seconds,
        "verification_seconds": verification_seconds,
        "bit_exact": bool(exact),
        "bytes": _directory_bytes(output),
        "status": "written",
    }


def _summary(seconds: list[float], raw_bytes_per_call: int | None = None) -> dict:
    values_ms = np.asarray(seconds, dtype=np.float64) * 1_000.0
    report = {
        "calls": int(len(seconds)),
        "total_seconds": float(np.sum(seconds)),
        "mean_ms": float(np.mean(values_ms)),
        "median_ms": float(np.median(values_ms)),
        "p95_ms": float(np.percentile(values_ms, 95)),
        "min_ms": float(np.min(values_ms)),
        "max_ms": float(np.max(values_ms)),
    }
    if raw_bytes_per_call is not None:
        report["decoded_mib_per_second"] = float(
            raw_bytes_per_call * len(seconds) / np.sum(seconds) / 2**20
        )
    return report


def _timed(callable_):
    started = time.perf_counter()
    value = callable_()
    elapsed = time.perf_counter() - started
    # Force lazy array-like results before stopping the logical operation.
    np.asarray(value)
    return elapsed


def _benchmark_store(
    store: AEFZarr,
    *,
    point_reads: int,
    patch_reads: int,
    block_reads: int,
    seed: int,
) -> tuple[dict, dict[str, list[float]]]:
    rng = np.random.default_rng(seed)
    margin = 4
    rows = rng.integers(margin, store.height - margin, size=max(point_reads, patch_reads))
    columns = rng.integers(margin, store.width - margin, size=max(point_reads, patch_reads))
    xs, ys = store.transform * (columns + 0.5, rows + 0.5)
    coords = list(zip(np.asarray(xs).tolist(), np.asarray(ys).tolist()))
    years = store.years

    # Untimed warm-up initializes codec and metadata machinery equally.
    store.sample_points([coords[0]], years=years, crs=store.crs)
    store.sample_patches([coords[0]], patch_size=9, years=years, crs=store.crs)
    store.read_window(0, store.height, 0, store.width, years=years)

    timings: dict[str, list[float]] = {"single_point": [], "patch_9x9": [], "block": []}
    point_order = rng.permutation(point_reads)
    for index in point_order:
        timings["single_point"].append(
            _timed(
                lambda index=index: store.sample_points(
                    [coords[index]], years=years, crs=store.crs
                )
            )
        )
    patch_order = rng.permutation(patch_reads)
    for index in patch_order:
        timings["patch_9x9"].append(
            _timed(
                lambda index=index: store.sample_patches(
                    [coords[index]], patch_size=9, years=years, crs=store.crs
                )
            )
        )
    for _ in range(block_reads):
        timings["block"].append(
            _timed(
                lambda: store.read_window(
                    0, store.height, 0, store.width, years=years
                )
            )
        )
    raw_block_bytes = int(np.prod(store.array.shape) * np.dtype(np.float32).itemsize)
    report = {
        "grid_id": store.grid_id,
        "years": years,
        "shape": list(store.array.shape),
        "inner_chunk": list(store.array.chunks),
        "single_point": _summary(timings["single_point"]),
        "patch_9x9": _summary(timings["patch_9x9"]),
        "block_256x256": _summary(timings["block"], raw_block_bytes),
    }
    return report, timings


def main() -> None:
    args = parse_args()
    chunks = sorted(set(args.chunks))
    if not chunks or any(chunk <= 0 or 512 % chunk for chunk in chunks):
        raise ValueError("Every chunk must be positive and divide shard size 512")
    if min(args.point_reads, args.patch_reads, args.block_reads) <= 0:
        raise ValueError("Read counts must be positive")
    sources = sorted(
        path.parent.parent
        for path in args.source_root.rglob("embeddings/zarr.json")
    )
    if not sources:
        raise ValueError(f"No source Zarr stores under {args.source_root}")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    builds = []
    results = []
    aggregate_timings = {
        chunk: {"single_point": [], "patch_9x9": [], "block": []}
        for chunk in chunks
    }
    for source_path in sources:
        source = AEFZarr(source_path)
        for chunk in chunks:
            output = args.out_dir / "stores" / f"chunk_{chunk}" / f"{source.grid_id}.zarr"
            build = _build_store(source, output, chunk, args.overwrite)
            build.update({"grid_id": source.grid_id, "chunk": chunk})
            builds.append(build)
            if not build["bit_exact"]:
                raise RuntimeError(f"Non-exact benchmark store: {output}")
            report, timings = _benchmark_store(
                AEFZarr(output),
                point_reads=args.point_reads,
                patch_reads=args.patch_reads,
                block_reads=args.block_reads,
                seed=args.seed + sum(map(ord, source.grid_id)),
            )
            report["chunk"] = chunk
            report["bytes"] = build["bytes"]
            results.append(report)
            for operation, values in timings.items():
                aggregate_timings[chunk][operation].extend(values)
            print(
                f"{source.grid_id} chunk={chunk} bytes={build['bytes']:,} "
                f"point={report['single_point']['median_ms']:.3f}ms "
                f"patch={report['patch_9x9']['median_ms']:.3f}ms "
                f"block={report['block_256x256']['median_ms']:.3f}ms",
                flush=True,
            )

    aggregates = {}
    for chunk in chunks:
        first_result = next(result for result in results if result["chunk"] == chunk)
        raw_block_bytes = int(
            np.prod(first_result["shape"]) * np.dtype(np.float32).itemsize
        )
        aggregates[str(chunk)] = {
            "stores": sum(result["chunk"] == chunk for result in results),
            "total_bytes": sum(
                result["bytes"] for result in results if result["chunk"] == chunk
            ),
            "single_point": _summary(aggregate_timings[chunk]["single_point"]),
            "patch_9x9": _summary(aggregate_timings[chunk]["patch_9x9"]),
            "block_256x256": _summary(
                aggregate_timings[chunk]["block"], raw_block_bytes
            ),
        }
    comparison = None
    if 32 in chunks and 64 in chunks:
        comparison = {
            "storage_ratio_64_over_32": (
                aggregates["64"]["total_bytes"] / aggregates["32"]["total_bytes"]
            ),
            "single_point_median_ratio_64_over_32": (
                aggregates["64"]["single_point"]["median_ms"]
                / aggregates["32"]["single_point"]["median_ms"]
            ),
            "patch_9x9_median_ratio_64_over_32": (
                aggregates["64"]["patch_9x9"]["median_ms"]
                / aggregates["32"]["patch_9x9"]["median_ms"]
            ),
            "block_median_ratio_64_over_32": (
                aggregates["64"]["block_256x256"]["median_ms"]
                / aggregates["32"]["block_256x256"]["median_ms"]
            ),
        }
    output = {
        "source_root": str(args.source_root.resolve()),
        "cache_condition": "warm operating-system cache; resolver and codec overhead",
        "seed": args.seed,
        "builds": builds,
        "per_store": results,
        "aggregate": aggregates,
        "comparison": comparison,
        "all_bit_exact": all(build["bit_exact"] for build in builds),
    }
    write_json(output, args.out_dir / "benchmark.json")
    print(json.dumps({"aggregate": aggregates, "comparison": comparison}, indent=2))


if __name__ == "__main__":
    main()
