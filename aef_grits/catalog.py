"""Spatial helpers shared by the point-registry and background samplers."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import shapely
from affine import Affine
from pyproj import Transformer
from shapely.geometry import box


def _affine(value) -> Affine:
    if isinstance(value, str):
        value = json.loads(value)
    values = list(value)
    if len(values) < 6:
        raise ValueError(f"Invalid catalog transform: {values}")
    return Affine(*map(float, values[:6]))


def _load_catalogs(catalog_path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """Load a root catalog and any immediate per-tile catalog supplements."""
    path = Path(catalog_path)
    root = path if path.is_dir() else path.parent
    paths = ([path] if path.is_file() else []) + sorted(
        child for child in root.glob("*/catalog.parquet") if child.is_file()
    )
    if not paths:
        raise FileNotFoundError(f"No catalog parquet found at {path}")
    return pd.concat([pd.read_parquet(item) for item in paths], ignore_index=True), [
        str(item) for item in paths
    ]


def _grid_records(catalog: pd.DataFrame, target_crs: str) -> list[dict]:
    required = {"tile_id", "crs", "transform", "width", "height"}
    missing = required.difference(catalog.columns)
    if missing:
        raise ValueError(f"Catalog missing columns: {sorted(missing)}")
    records: list[dict] = []
    seen: set[tuple] = set()
    for _, row in catalog.iterrows():
        native_crs = str(row["crs"])
        transform = _affine(row["transform"])
        width, height = int(row["width"]), int(row["height"])
        key = (str(row["tile_id"]), native_crs, tuple(transform)[:6], width, height)
        if key in seen:
            continue
        seen.add(key)
        corners = [
            transform * (column, line)
            for column, line in ((0, 0), (width, 0), (0, height), (width, height))
        ]
        xs, ys = zip(*corners)
        native_footprint = box(min(xs), min(ys), max(xs), max(ys))
        to_target = Transformer.from_crs(native_crs, target_crs, always_xy=True)
        from_target = Transformer.from_crs(target_crs, native_crs, always_xy=True)
        records.append(
            {
                "tile_id": str(row["tile_id"]),
                "transform": transform,
                "width": width,
                "height": height,
                "crs": native_crs,
                "footprint": shapely.transform(
                    native_footprint, to_target.transform, interleaved=False
                ),
                "to_target": to_target,
                "from_target": from_target,
                "resolution_x": abs(float(transform.a)),
                "resolution_y": abs(float(transform.e)),
            }
        )
    if not records:
        raise ValueError(f"Catalog contains no grids in target CRS {target_crs}")
    return records


def load_catalog_grids(
    catalog_path: str | Path, target_crs: str = "EPSG:32717"
) -> tuple[list[dict], list[str]]:
    catalog, sources = _load_catalogs(catalog_path)
    return _grid_records(catalog, target_crs), sources


def choose_grid(x: float, y: float, grids: list[dict]) -> dict | None:
    point = shapely.Point(x, y)
    candidates = [grid for grid in grids if grid["footprint"].covers(point)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda grid: grid["footprint"].boundary.distance(point),
    )


def spatial_block_id(x: float, y: float, block_size_m: float = 6000.0) -> str:
    if block_size_m <= 0:
        raise ValueError("block_size_m must be positive")
    return f"g{math.floor(x / block_size_m)}_{math.floor(y / block_size_m)}"
