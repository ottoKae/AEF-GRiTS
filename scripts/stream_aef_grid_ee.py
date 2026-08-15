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
import os
import shutil
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd
from rasterio.transform import Affine
import zarr
from zarr.codecs import BloscCodec


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet  # noqa: E402
from aef_grits.earth_engine import (  # noqa: E402
    AEF_BANDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DATASET,
    EarthEngineRequestError,
    RequestPolicy,
    annual_image,
    execute_with_retry,
    initialize,
)
from aef_grits.grids import (  # noqa: E402
    AEF_RESOLUTION_M,
    GridSpec,
    MGRSGridProvider,
    ReferenceGridProvider,
    Tessera01GridProvider,
)
from aef_grits.resource_budget import (  # noqa: E402
    DEFAULT_GLOBAL_REQUESTS,
    MemoryGuard,
    ResourceLimitError,
    memory_snapshot,
    plan_grid_resources,
    resolve_resource_profile,
)
from aef_grits.resource_control import TokenPool, default_resource_root  # noqa: E402
from aef_grits.storage_safety import (  # noqa: E402
    adopt_existing_store,
    commit_staged_tree,
    mount_for_path,
    validate_storage_layout,
)
from aef_grits.telemetry import RunTelemetry  # noqa: E402


NODATA = -9999.0
BYTES_PER_VALUE = 4
COMPUTE_PIXELS_LIMIT = 48_000_000
COMPRESSION_CNAME = "zstd"
COMPRESSION_LEVEL = 7
COMPRESSION_SHUFFLE = "noshuffle"
COMPRESSION_TYPESIZE = 4
COMPRESSION_PROTOCOL_VERSION = 2
EVENT_PREFIX = "AEF_EVENT "
DEFAULT_INNER_CHUNK = 64
DEFAULT_SHARD_SIZE = 512


def emit_event(event: str, **payload) -> None:
    print(EVENT_PREFIX + json.dumps({"event": event, **payload}, separators=(",", ":")), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grid-scheme",
        choices=("reference", "tessera_0p1", "mgrs"),
        default="reference",
    )
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--tile-id")
    parser.add_argument(
        "--tessera-tile",
        nargs=2,
        type=float,
        action="append",
        metavar=("LON", "LAT"),
        help="Tessera 0.1-degree tile centre; may be repeated",
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
    )
    parser.add_argument(
        "--mgrs-index",
        type=Path,
        help="Optional override; defaults to the packaged global MGRS index",
    )
    parser.add_argument("--tiles", nargs="+")
    parser.add_argument("--project", required=True)
    parser.add_argument("--years", nargs="+", type=int, default=list(range(2017, 2026)))
    parser.add_argument("--block-size", type=int, default=256)
    parser.add_argument("--inner-chunk", type=int, default=DEFAULT_INNER_CHUNK)
    parser.add_argument("--shard-size", type=int, default=DEFAULT_SHARD_SIZE)
    parser.add_argument("--workers", default="auto", help="Positive integer or auto")
    parser.add_argument(
        "--resource-profile",
        choices=("workstation-auto", "low-memory-1g", "server-8g", "server-16g"),
        default="workstation-auto",
    )
    parser.add_argument("--memory-limit-gib")
    parser.add_argument("--memory-reserve-gib")
    parser.add_argument("--global-request-limit", type=int)
    parser.add_argument(
        "--resource-state-dir",
        type=Path,
        help="Native shared root for cross-process request tokens and delivery lock",
    )
    parser.add_argument("--memory-high-watermark", type=float, default=0.80)
    parser.add_argument("--memory-critical-watermark", type=float, default=0.90)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Deadline for each Earth Engine API attempt (default: 300)",
    )
    parser.add_argument("--checkpoint-every", type=int, default=8)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Native-filesystem root for checkpoints, catalogs and reports",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        help="Native-filesystem root used to build each grid before final delivery",
    )
    parser.add_argument(
        "--commit-timeout",
        type=float,
        default=21_600.0,
        help="Maximum seconds allowed for the isolated final-store copy",
    )
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="Keep a successfully delivered native-filesystem staging copy",
    )
    parser.add_argument(
        "--adopt-existing-complete",
        action="store_true",
        help="Finalize a legacy store only when its external ledger is already complete",
    )
    parser.add_argument(
        "--import-legacy-progress",
        type=Path,
        help="One legacy progress JSON, or a directory containing per-store ledgers; never deleted",
    )
    parser.add_argument("--high-volume", action="store_true")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Resolve grids, outputs and uncompressed size without contacting Earth Engine",
    )
    return parser.parse_args()


def reference_grid(path: Path) -> dict:
    """Backward-compatible helper returning a validated 10 m reference grid."""
    return ReferenceGridProvider(path, path.stem).get().as_dict()


def resolve_grids(args: argparse.Namespace) -> list[GridSpec]:
    """Resolve command-line selection into one or more authoritative grids."""
    if args.grid_scheme == "reference":
        if args.reference is None or not args.tile_id:
            raise ValueError("reference mode requires --reference and --tile-id")
        grids = [ReferenceGridProvider(args.reference, args.tile_id).get()]
    elif args.grid_scheme == "tessera_0p1":
        provider = Tessera01GridProvider()
        if bool(args.tessera_tile) == bool(args.bbox):
            raise ValueError(
                "tessera_0p1 mode requires exactly one of --tessera-tile or --bbox"
            )
        if args.tessera_tile:
            grids = [
                provider.get(lon, lat, coordinates_are_centres=True)
                for lon, lat in args.tessera_tile
            ]
        else:
            grids = provider.from_bbox(args.bbox)
    else:
        tile_ids = list(args.tiles or ([] if args.tile_id is None else [args.tile_id]))
        grids = MGRSGridProvider(args.mgrs_index).get_many(tile_ids)
    ids = [grid.grid_id for grid in grids]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate grid IDs requested: {ids}")
    return grids


def output_paths(
    grids: list[GridSpec], years: list[int], out: Path | None, out_dir: Path | None
) -> dict[str, Path]:
    if len(grids) == 1 and out is not None:
        return {grids[0].grid_id: out}
    if out is not None:
        raise ValueError("--out is only valid for one grid; use --out-dir")
    if out_dir is None:
        raise ValueError("Select --out for one grid or --out-dir for one or more grids")
    year_token = f"{min(years)}_{max(years)}"
    return {
        grid.grid_id: out_dir / grid.grid_id / f"aef_{grid.grid_id}_{year_token}.zarr"
        for grid in grids
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
        "scheme": grid.get("scheme", "reference"),
        "tile_id": tile_id,
        "years": years,
        "crs": grid["crs"],
        "transform": list(grid["transform"])[:6],
        "width": grid["width"],
        "height": grid["height"],
        "block_size": block_size,
        "inner_chunk": inner_chunk,
        "shard_size": shard_size,
        "compression": {
            "protocol_version": COMPRESSION_PROTOCOL_VERSION,
            "codec": COMPRESSION_CNAME,
            "level": COMPRESSION_LEVEL,
            "shuffle": COMPRESSION_SHUFFLE,
            "typesize": COMPRESSION_TYPESIZE,
        },
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


def _fetch_block(
    ee,
    image,
    grid: dict,
    block: dict,
    max_retries: int,
    request_pool,
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    telemetry: RunTelemetry | None = None,
):
    request = {
        "expression": image,
        "fileFormat": "NUMPY_NDARRAY",
        "bandIds": list(AEF_BANDS),
        "grid": _pixel_grid(grid, block),
        "workloadTag": "aef_grits_grid",
    }
    def fetch():
        token_context = telemetry.request_token(request_pool) if telemetry else request_pool.token()
        with token_context:
            return ee.data.computePixels(request)

    response = execute_with_retry(
        f"computePixels:{block['key']}",
        fetch,
        RequestPolicy(
            timeout_seconds=request_timeout_seconds,
            max_retries=max_retries,
        ),
        event=lambda name, payload: (
            telemetry.request_event(name, payload) if telemetry else None,
            emit_event(name, workflow="grid", **payload),
        ),
    )
    try:
        values = np.stack(
            [np.asarray(response[band], dtype=np.float32) for band in AEF_BANDS]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Earth Engine response schema is invalid for block {block['key']}: {exc}"
        ) from exc
    values[values == NODATA] = np.nan
    if telemetry is not None:
        telemetry.add_received_bytes(values.nbytes)
    return block, values


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
    compressor = BloscCodec(
        cname=COMPRESSION_CNAME,
        clevel=COMPRESSION_LEVEL,
        shuffle="noshuffle",
        typesize=COMPRESSION_TYPESIZE,
    )
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
            "aef:grid_scheme": grid.get("scheme", "reference"),
            "aef:resolution_m": AEF_RESOLUTION_M,
            "aef:compression_protocol_version": COMPRESSION_PROTOCOL_VERSION,
            "aef:compression_codec": COMPRESSION_CNAME,
            "aef:compression_level": COMPRESSION_LEVEL,
            "aef:compression_shuffle": COMPRESSION_SHUFFLE,
            "aef:compression_typesize": COMPRESSION_TYPESIZE,
            "geoemb:model": "AlphaEarth Foundations",
            "geoemb:dimensions": len(AEF_BANDS),
            "tile_id": grid.get("tile_id"),
            "grid_id": grid.get("grid_id", grid.get("tile_id")),
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
        if {"scheme", "grid_id"}.issubset(catalog.columns):
            keep = ~(
                catalog.scheme.astype(str).eq(str(record["scheme"]))
                & catalog.grid_id.astype(str).eq(str(record["grid_id"]))
            )
            catalog = catalog.loc[keep]
        elif "tile_id" in catalog:
            catalog = catalog[catalog.tile_id.astype(str) != str(record["tile_id"])]
        catalog = pd.concat([catalog, pd.DataFrame([record])], ignore_index=True)
    else:
        catalog = pd.DataFrame([record])
    write_parquet(catalog, path)


def _stream_one(
    args: argparse.Namespace,
    years: list[int],
    grid_spec: GridSpec,
    output: Path,
    catalog_path: Path,
    state_root: Path,
    staging_root: Path | None,
    ee,
    resource_plan=None,
    request_pool=None,
    resource_root=None,
    telemetry=None,
) -> dict:
    grid = grid_spec.as_dict()
    if resource_plan is None:
        resource_plan = plan_grid_resources(
            block_size=args.block_size,
            workers=args.workers,
            snapshot=memory_snapshot(),
        )
    if resource_root is None:
        resource_root = default_resource_root(state_root)
    if request_pool is None:
        request_pool = TokenPool(
            Path(resource_root) / "earth_engine",
            resource_plan.global_request_limit,
            name="request",
        )
    request_bytes = args.block_size**2 * len(AEF_BANDS) * BYTES_PER_VALUE
    signature = _signature(
        grid,
        years,
        grid_spec.grid_id,
        block_size=args.block_size,
        inner_chunk=args.inner_chunk,
        shard_size=args.shard_size,
    )
    grid_state = state_root / "grids" / grid_spec.grid_id
    grid_state.mkdir(parents=True, exist_ok=True)
    progress_path = grid_state / f"{output.name}.progress.json"
    if not progress_path.exists() and args.import_legacy_progress is not None:
        legacy = args.import_legacy_progress
        if legacy.is_dir():
            legacy = legacy / progress_path.name
        imported = json.loads(legacy.read_text(encoding="utf-8"))
        if imported.get("signature") != signature:
            raise ValueError("Legacy progress signature differs from the requested store")
        imported["imported_from"] = str(legacy)
        imported["delivery_status"] = imported.get("delivery_status", "staging")
        write_json(imported, progress_path)
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress.get("signature") != signature:
            raise ValueError("Progress signature differs; use a new output path")
    else:
        progress = {"signature": signature, "completed": [], "delivery_status": "staging"}
    completed = set(progress.get("completed", []))
    memory_guard = MemoryGuard(resource_plan)
    telemetry = telemetry or RunTelemetry()
    all_blocks = list(_block_specs(grid, years, args.block_size))
    already_committed = progress.get("delivery_status") == "committed"
    if already_committed:
        completed = {block["key"] for block in all_blocks}
    work_output = (
        staging_root / grid_spec.grid_id / output.name
        if staging_root is not None
        else output
    )
    adopting = False
    if (
        not already_committed
        and args.adopt_existing_complete
        and len(completed) == len(all_blocks)
        and work_output != output
    ):
        adopt_existing_store(
            output,
            signature=signature,
            expected_shape=(len(years), len(AEF_BANDS), grid["height"], grid["width"]),
            state_dir=grid_state,
            timeout_seconds=min(args.commit_timeout, 600.0),
        )
        already_committed = True
        adopting = True
    elif not already_committed and completed and not work_output.exists():
        raise FileNotFoundError(
            f"The external ledger records {len(completed)} blocks but the working "
            f"store is missing: {work_output}. Restore staging, or use "
            "--adopt-existing-complete only for a fully completed legacy store."
        )
    target = None
    images = {}
    if not already_committed:
        store = _create_store(
            work_output,
            grid,
            years,
            signature,
            args.inner_chunk,
            args.shard_size,
        )
        target = store["embeddings"]
        xmin, ymin, xmax, ymax = grid["bounds"]
        bounds = ee.Geometry.Rectangle(
            [xmin, ymin, xmax, ymax], proj=grid["crs"], geodesic=False
        )
        images = {
            year: annual_image(year, bounds).unmask(NODATA, sameFootprint=False)
            for year in years
        }
    pending = [block for block in all_blocks if block["key"] not in completed]
    print(
        json.dumps(
            {
                "scheme": grid_spec.scheme,
                "grid_id": grid_spec.grid_id,
                "shape": [len(years), len(AEF_BANDS), grid["height"], grid["width"]],
                "zarr_chunks": [1, len(AEF_BANDS), args.inner_chunk, args.inner_chunk],
                "zarr_shards": [1, len(AEF_BANDS), args.shard_size, args.shard_size],
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
            pressure = memory_guard.sample()
            if pressure == "critical":
                write_json(
                    {
                        "signature": signature,
                        "completed": sorted(completed),
                        "delivery_status": "staging",
                        "working_store": str(work_output),
                        "final_store": str(output),
                        "resource_limit_stop": True,
                    },
                    progress_path,
                )
                raise ResourceLimitError("Critical memory watermark reached; checkpoint saved")
            if pressure == "high" and active:
                return False
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
                request_pool,
                getattr(args, "request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS),
                telemetry,
            )
            active[future] = block
            return True

        for _ in range(resource_plan.max_in_flight):
            if not submit_next():
                break
        written = 0
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                active.pop(future)
                block, values = future.result()
                row, column = block["row"], block["column"]
                assert target is not None
                with telemetry.measure_write():
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
                        {
                            "signature": signature,
                            "completed": sorted(completed),
                            "delivery_status": "staging",
                            "working_store": str(work_output),
                            "final_store": str(output),
                        },
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
                emit_event(
                    "progress",
                    workflow="grid",
                    grid_id=grid_spec.grid_id,
                    completed=len(completed),
                    total=len(all_blocks),
                    eta_seconds=None if not math.isfinite(eta) else eta,
                    rate_per_second=rate,
                )
                submit_next()
    write_json(
        {
            "signature": signature,
            "completed": sorted(completed),
            "delivery_status": "staging",
            "working_store": str(work_output),
            "final_store": str(output),
        },
        progress_path,
    )
    if len(completed) != len(all_blocks):
        raise RuntimeError("Run ended before every block was committed")

    if not already_committed:
        zarr.consolidate_metadata(str(work_output))
    completion = {
        "signature": signature,
        "grid_id": grid_spec.grid_id,
        "blocks": len(all_blocks),
        "years": years,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if already_committed:
        completion["reconciled_committed_store"] = True
    elif work_output != output:
        write_json(completion, work_output / "AEF_COMPLETE.json")
        validate_storage_layout(output.parent, state_root, staging_root)
        with telemetry.measure_delivery():
            commit_staged_tree(
                work_output,
                output,
                signature=signature,
                state_dir=grid_state,
                timeout_seconds=args.commit_timeout,
                resource_root=resource_root,
            )
    if adopting:
        completion["adopted_legacy_store"] = True
    write_json(
        {
            "signature": signature,
            "completed": sorted(completed),
            "delivery_status": "committed",
            "legacy_adopted": adopting or bool(progress.get("legacy_adopted")),
            "working_store": str(work_output),
            "final_store": str(output),
        },
        progress_path,
    )
    record = {
        "scheme": grid_spec.scheme,
        "grid_id": grid_spec.grid_id,
        "tile_id": grid_spec.grid_id,
        "product_type": "aef_annual",
        "zarr_path": os.path.abspath(output),
        "crs": grid["crs"],
        "transform": json.dumps(list(grid["transform"])[:6]),
        "width": grid["width"],
        "height": grid["height"],
        "bounds": json.dumps(list(grid["bounds"])),
        "resolution_m": AEF_RESOLUTION_M,
        "compression_protocol_version": COMPRESSION_PROTOCOL_VERSION,
        "compression_codec": COMPRESSION_CNAME,
        "compression_level": COMPRESSION_LEVEL,
        "compression_shuffle": COMPRESSION_SHUFFLE,
        "compression_typesize": COMPRESSION_TYPESIZE,
        "years": json.dumps(years),
        "bands": json.dumps(list(AEF_BANDS)),
        "grid_metadata": json.dumps(grid.get("metadata", {}), sort_keys=True),
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
    report_path = grid_state / f"{output.name}.report.json"
    report["report"] = str(report_path.resolve())
    report["working_store"] = os.path.abspath(work_output)
    report["adopted_legacy_store"] = adopting
    report["resource_plan"] = resource_plan.as_dict()
    report["request_policy"] = RequestPolicy(
        timeout_seconds=getattr(
            args, "request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
        max_retries=args.max_retries,
    ).as_dict()
    report["telemetry"] = telemetry.report()
    report["resource_profile"] = getattr(args, "resolved_resource_profile", None)
    report.update(memory_guard.report())
    write_json(report, report_path)
    if work_output != output and work_output.exists() and not args.keep_staging:
        shutil.rmtree(work_output)
    print(json.dumps(report, indent=2))
    return report


def main() -> None:
    args = parse_args()
    profile = resolve_resource_profile(
        args.resource_profile,
        memory_limit_gib=args.memory_limit_gib,
        memory_reserve_gib=args.memory_reserve_gib,
        global_request_limit=args.global_request_limit,
    )
    args.memory_limit_gib = profile["memory_limit_gib"]
    args.memory_reserve_gib = profile["memory_reserve_gib"]
    args.global_request_limit = profile["global_request_limit"]
    args.resolved_resource_profile = profile
    years = sorted(set(args.years))
    if not years:
        raise ValueError("At least one year is required")
    for name in ("block_size", "inner_chunk", "shard_size", "checkpoint_every"):
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if args.shard_size % args.inner_chunk:
        raise ValueError("shard-size must be divisible by inner-chunk")
    if args.commit_timeout <= 0:
        raise ValueError("commit-timeout must be positive")
    if args.request_timeout_seconds <= 0:
        raise ValueError("request-timeout-seconds must be positive")
    if args.global_request_limit <= 0:
        raise ValueError("global-request-limit must be positive")
    request_bytes = args.block_size**2 * len(AEF_BANDS) * BYTES_PER_VALUE
    if request_bytes > COMPUTE_PIXELS_LIMIT:
        raise ValueError(
            f"block-size={args.block_size} requests {request_bytes / 1e6:.1f} MB; "
            "Earth Engine computePixels allows at most 48 MB uncompressed"
        )

    snapshot = memory_snapshot(
        limit_gib=args.memory_limit_gib,
        reserve_gib=args.memory_reserve_gib,
    )
    resource_plan = plan_grid_resources(
        block_size=args.block_size,
        workers=args.workers,
        snapshot=snapshot,
        global_request_limit=args.global_request_limit,
        high_watermark=args.memory_high_watermark,
        critical_watermark=args.memory_critical_watermark,
    )
    args.workers = resource_plan.workers
    grids = resolve_grids(args)
    outputs = output_paths(grids, years, args.out, args.out_dir)
    final_root = args.out_dir or next(iter(outputs.values())).parent
    state_root = args.state_dir or final_root / ".aef_state"
    layout = validate_storage_layout(
        final_root,
        state_root,
        args.staging_dir,
    )
    state_root = Path(layout.state_root)
    staging_root = Path(layout.staging_root) if layout.staging_root else None
    resource_root = args.resource_state_dir or default_resource_root(state_root)
    resource_root = Path(resource_root).expanduser().absolute()
    resource_mount = mount_for_path(resource_root)
    if resource_mount and resource_mount.is_linux_ntfs:
        raise ValueError("Shared resource state must be on a native filesystem")
    if layout.final_mount and layout.final_mount.is_linux_ntfs and resource_mount is None:
        raise ValueError("Cannot verify the shared resource-state filesystem for NTFS output")
    resource_root.mkdir(parents=True, exist_ok=True)
    request_pool = TokenPool(
        resource_root / "earth_engine",
        resource_plan.global_request_limit,
        name="request",
    )
    catalog_path = args.catalog or state_root / "catalog.parquet"
    catalog_mount = mount_for_path(catalog_path)
    if layout.final_mount and layout.final_mount.is_linux_ntfs:
        if catalog_mount is None or catalog_mount.is_linux_ntfs:
            raise ValueError("Catalog must be on the native --state-dir for NTFS output")
    if args.plan_only:
        root = staging_root or state_root
        raw_bytes = sum(
            len(years) * len(AEF_BANDS) * grid.width * grid.height * BYTES_PER_VALUE
            for grid in grids
        )
        free_bytes = shutil.disk_usage(root).free
        plan = {
            "scheme": args.grid_scheme,
            "project": args.project,
            "years": years,
            "grid_count": len(grids),
            "grids": [
                {
                    "grid_id": grid.grid_id,
                    "crs": grid.crs,
                    "width": grid.width,
                    "height": grid.height,
                    "output": str(outputs[grid.grid_id].resolve()),
                }
                for grid in grids
            ],
            "raw_bytes": raw_bytes,
            "raw_gib": raw_bytes / 1024**3,
            "free_bytes": free_bytes,
            "free_gib": free_bytes / 1024**3,
            "storage_layout": layout.as_dict(),
            "memory": snapshot.as_dict(),
            "resource_plan": resource_plan.as_dict(),
            "resource_state_dir": str(resource_root),
            "request_policy": RequestPolicy(
                timeout_seconds=args.request_timeout_seconds,
                max_retries=args.max_retries,
            ).as_dict(),
            "resource_profile": profile,
            "note": "raw_gib is uncompressed float32 size; lossless Zarr size depends on feature entropy",
        }
        print(json.dumps(plan, indent=2))
        return
    ee = initialize(
        args.project,
        high_volume=args.high_volume,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    reports = []
    failures = []
    resumable_failure = None
    run_telemetry = RunTelemetry()
    for index, grid in enumerate(grids, 1):
        print(f"Grid {index}/{len(grids)}: {grid.scheme}/{grid.grid_id}", flush=True)
        emit_event(
            "grid_start",
            workflow="grid",
            grid_id=grid.grid_id,
            grid_index=index,
            grid_total=len(grids),
            crs=grid.crs,
        )
        try:
            report = _stream_one(
                args,
                years,
                grid,
                outputs[grid.grid_id],
                catalog_path,
                state_root,
                staging_root,
                ee,
                resource_plan,
                request_pool,
                resource_root,
                run_telemetry,
            )
            reports.append(report)
            emit_event(
                "grid_complete",
                workflow="grid",
                grid_id=grid.grid_id,
                grid_index=index,
                grid_total=len(grids),
            )
        except Exception as exc:
            failures.append({"grid_id": grid.grid_id, "error": str(exc)})
            if isinstance(exc, EarthEngineRequestError) and exc.resumable:
                resumable_failure = resumable_failure or exc
            print(f"Grid failed: {grid.grid_id}: {exc}", file=sys.stderr, flush=True)
    summary = {
        "scheme": args.grid_scheme,
        "requested": len(grids),
        "completed": len(reports),
        "failed": failures,
        "catalog": str(catalog_path.resolve()),
    }
    summary["storage_layout"] = layout.as_dict()
    summary["memory"] = snapshot.as_dict()
    summary["resource_plan"] = resource_plan.as_dict()
    summary["request_policy"] = RequestPolicy(
        timeout_seconds=args.request_timeout_seconds,
        max_retries=args.max_retries,
    ).as_dict()
    summary["resource_profile"] = profile
    summary["telemetry"] = run_telemetry.report()
    summary["resource_state_dir"] = str(resource_root)
    write_json(summary, state_root / "run_summary.json")
    emit_event("run_complete", workflow="grid", completed=len(reports), total=len(grids))
    if resumable_failure is not None:
        raise resumable_failure
    if failures:
        raise RuntimeError(f"{len(failures)} of {len(grids)} grids failed")


if __name__ == "__main__":
    try:
        main()
    except EarthEngineRequestError as exc:
        emit_event(
            "run_resumable_failure" if exc.resumable else "run_failed",
            workflow="grid",
            operation=exc.operation,
            classification=exc.classification,
            attempts=exc.attempts,
            error=str(exc),
        )
        raise SystemExit(75 if exc.resumable else 1) from exc
