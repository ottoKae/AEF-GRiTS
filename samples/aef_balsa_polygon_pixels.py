#!/usr/bin/env python
"""Build deterministic, polygon-equal AEF point samples for all BALSA stands.

The output is model independent.  It keeps every source BALSA polygon, places
one robust anchor plus a capped spatially distributed set of interior points,
and assigns every point a reciprocal polygon weight.  The point CSV can be
passed directly to the Earth Engine annual AEF exporter.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import geopandas as gpd
from mgrs import MGRS
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point
from shapely.ops import polylabel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aef_grits.catalog import choose_grid, load_catalog_grids  # noqa: E402

DEFAULT_OUT_DIR = PROJECT_ROOT / "outputs/polygon_inventory"
DEFAULT_11_TILES = (
    "17MNT", "17MNU", "17MNV", "17MPT", "17MPU", "17MPV",
    "17MQV", "17NPA", "17NQA", "17NQB", "17NRA",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shapefile", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--class-column", default="nco")
    parser.add_argument("--class-value", default="BALSA")
    parser.add_argument(
        "--hectares-per-point", type=float, default=0.25,
        help="Adaptive sampling density before applying --max-points.",
    )
    parser.add_argument("--max-points", type=int, default=25)
    parser.add_argument("--boundary-buffer-m", type=float, default=10.0)
    parser.add_argument("--polylabel-tolerance-m", type=float, default=1.0)
    parser.add_argument("--tiles", nargs="+", default=list(DEFAULT_11_TILES))
    parser.add_argument(
        "--s1-catalog", type=Path,
        help="Optional onlyTK catalog; enables exact current-11-grid coverage assignment.",
    )
    return parser.parse_args()


def _repair_geometry(geometry):
    if geometry is None or geometry.is_empty:
        return geometry, "empty"
    if geometry.is_valid:
        return geometry, "not_needed"
    try:
        from shapely import make_valid

        repaired = make_valid(geometry)
    except (ImportError, AttributeError):
        repaired = geometry.buffer(0)
    if repaired is None or repaired.is_empty:
        return repaired, "failed"
    return repaired, "repaired" if repaired.is_valid else "still_invalid"


def _largest_polygon(geometry):
    if geometry.geom_type == "Polygon":
        return geometry
    polygons = [part for part in getattr(geometry, "geoms", ()) if part.geom_type == "Polygon"]
    if not polygons:
        return None
    return max(polygons, key=lambda item: item.area)


def _anchor_point(geometry, tolerance: float) -> Point:
    polygon = _largest_polygon(geometry)
    if polygon is None:
        return geometry.representative_point()
    try:
        return polylabel(polygon, tolerance=max(float(tolerance), 0.01))
    except Exception:
        return polygon.representative_point()


def _candidate_grid(geometry, target: int) -> list[Point]:
    """Create deterministic candidate points, densifying for narrow polygons."""
    minx, miny, maxx, maxy = geometry.bounds
    width = max(maxx - minx, 1e-6)
    height = max(maxy - miny, 1e-6)
    aspect = width / height
    density = max(4 * target, 16)
    base_x = max(2, int(math.ceil(math.sqrt(density * aspect))))
    base_y = max(2, int(math.ceil(density / base_x)))
    points: list[Point] = []
    for multiplier in (1, 2, 4, 8):
        nx, ny = base_x * multiplier, base_y * multiplier
        xs = minx + (np.arange(nx) + 0.5) * width / nx
        ys = miny + (np.arange(ny) + 0.5) * height / ny
        points = [Point(float(x), float(y)) for y in ys for x in xs if geometry.covers(Point(float(x), float(y)))]
        if len(points) >= target or multiplier == 8:
            break
    points.sort(key=lambda point: (point.x, point.y))
    return points


def _farthest_points(candidates: list[Point], anchor: Point, target: int) -> list[Point]:
    if target <= 1 or not candidates:
        return [anchor]
    coordinates = np.array([(point.x, point.y) for point in candidates], dtype=np.float64)
    selected = [(float(anchor.x), float(anchor.y))]
    distance = np.sum((coordinates - np.asarray(selected[0])) ** 2, axis=1)
    for _ in range(target - 1):
        index = int(np.argmax(distance))
        if distance[index] <= 1e-12:
            break
        chosen = coordinates[index]
        selected.append((float(chosen[0]), float(chosen[1])))
        distance = np.minimum(distance, np.sum((coordinates - chosen) ** 2, axis=1))
        distance[index] = -1.0
    return [Point(x, y) for x, y in selected]


def _mgrs_tile(mgrs_encoder: MGRS, latitude: float, longitude: float) -> str:
    value = mgrs_encoder.toMGRS(float(latitude), float(longitude), MGRSPrecision=0)
    return value.decode("ascii") if isinstance(value, bytes) else str(value)


def build_inventory(
    shapefile: str | Path,
    *,
    class_column: str = "nco",
    class_value: str = "BALSA",
    hectares_per_point: float = 0.25,
    max_points: int = 25,
    boundary_buffer_m: float = 10.0,
    polylabel_tolerance_m: float = 1.0,
    analysis_tiles: tuple[str, ...] = DEFAULT_11_TILES,
    s1_catalog: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    if hectares_per_point <= 0 or max_points <= 0 or boundary_buffer_m < 0:
        raise ValueError("sampling density/max-points/buffer arguments are invalid")
    source = gpd.read_file(shapefile)
    if source.crs is None:
        raise ValueError("source shapefile has no CRS")
    if class_column not in source:
        raise ValueError(f"missing class column: {class_column}")
    normalized = source[class_column].astype("string").str.strip().str.upper()
    target_value = str(class_value).strip().upper()
    balsa = source.loc[normalized.eq(target_value)].copy()
    if balsa.empty:
        raise ValueError(f"no rows matched normalized {class_column}={target_value}")

    # Distances and areas must be measured in metres.  The official file is
    # EPSG:32717, but reproject explicitly when another source is supplied.
    metric = balsa.to_crs("EPSG:32717")
    to_wgs84 = Transformer.from_crs("EPSG:32717", "EPSG:4326", always_xy=True)
    mgrs_encoder = MGRS()
    tile_set = {str(value).upper() for value in analysis_tiles}
    grids, catalog_sources = (load_catalog_grids(s1_catalog) if s1_catalog else ([], []))
    point_records: list[dict[str, object]] = []
    polygon_records: list[dict[str, object]] = []

    for position, (source_index, source_row) in enumerate(balsa.iterrows()):
        geometry, repair_status = _repair_geometry(metric.iloc[position].geometry)
        if geometry is None or geometry.is_empty:
            polygon_records.append(
                {
                    "polygon_id": f"balsa_{source_index}", "source_index": source_index,
                    "geometry_repair_status": repair_status, "n_points": 0,
                    "extraction_status": "unusable_geometry",
                }
            )
            continue
        area_ha = float(geometry.area / 10_000.0)
        target_points = min(max_points, max(1, int(math.ceil(area_ha / hectares_per_point))))
        eroded = geometry.buffer(-boundary_buffer_m) if boundary_buffer_m > 0 else geometry
        used_buffer = bool(eroded is not None and not eroded.is_empty)
        sampling_geometry = eroded if used_buffer else geometry
        anchor = _anchor_point(sampling_geometry, polylabel_tolerance_m)
        candidates = _candidate_grid(sampling_geometry, target_points)
        points = _farthest_points(candidates, anchor, target_points)
        lon, lat = to_wgs84.transform(
            np.array([point.x for point in points]), np.array([point.y for point in points])
        )
        point_tiles = [
            _mgrs_tile(mgrs_encoder, latitude, longitude)
            for longitude, latitude in zip(lon, lat)
        ]
        polygon_tile = point_tiles[0]
        s1_grids = [choose_grid(float(point.x), float(point.y), grids) for point in points]
        point_s1_tiles = [grid["tile_id"] if grid is not None else None for grid in s1_grids]
        anchor_s1_tile = point_s1_tiles[0]
        inside_11_grids = anchor_s1_tile is not None
        polygon_id = f"balsa_{source_index}"
        n_points = len(points)
        attributes = {
            "source_objectid": source_row.get("OBJECTID"),
            "source_csp": source_row.get("csp"),
            "source_sce_ha": source_row.get("sce"),
            "province": source_row.get("DPA_DESPRO"),
            "canton": source_row.get("DPA_DESCAN"),
            "parish": source_row.get("DPA_DESPAR"),
        }
        for rank, (point, longitude, latitude, point_tile, point_s1_tile) in enumerate(
            zip(points, lon, lat, point_tiles, point_s1_tiles), start=1
        ):
            point_records.append(
                {
                    "sample_id": f"{polygon_id}_px{rank:02d}",
                    "polygon_id": polygon_id,
                    "source_index": source_index,
                    "point_rank": rank,
                    "is_anchor": rank == 1,
                    "model_center_x": float(point.x),
                    "model_center_y": float(point.y),
                    "lon": float(longitude),
                    "lat": float(latitude),
                    "tile_id": point_tile,
                    "polygon_tile_id": polygon_tile,
                    "s1_tile_id": point_s1_tile,
                    "polygon_s1_tile_id": anchor_s1_tile,
                    "inside_11_tiles": polygon_tile in tile_set,
                    "inside_11_s1_grids": inside_11_grids,
                    "polygon_area_ha": area_ha,
                    "target_points": target_points,
                    "n_points_polygon": n_points,
                    "polygon_equal_weight": 1.0 / n_points,
                    "distance_to_boundary_m": float(point.distance(geometry.boundary)),
                    "boundary_buffer_requested_m": boundary_buffer_m,
                    "boundary_buffer_applied": used_buffer,
                    "source_crs": "EPSG:32717",
                    "label": 1,
                    **attributes,
                }
            )
        polygon_records.append(
            {
                "polygon_id": polygon_id,
                "source_index": source_index,
                "tile_id": polygon_tile,
                "s1_tile_id": anchor_s1_tile,
                "inside_11_tiles": polygon_tile in tile_set,
                "inside_11_s1_grids": inside_11_grids,
                "polygon_area_ha": area_ha,
                "target_points": target_points,
                "n_points": n_points,
                "anchor_x": float(anchor.x),
                "anchor_y": float(anchor.y),
                "anchor_lon": float(lon[0]),
                "anchor_lat": float(lat[0]),
                "geometry_repair_status": repair_status,
                "boundary_buffer_applied": used_buffer,
                "extraction_status": "ready",
                **attributes,
            }
        )

    points = pd.DataFrame(point_records)
    polygons = pd.DataFrame(polygon_records)
    if points["sample_id"].duplicated().any() or polygons["polygon_id"].duplicated().any():
        raise AssertionError("generated IDs are not unique")
    weights = points.groupby("polygon_id")["polygon_equal_weight"].sum()
    if not np.allclose(weights.to_numpy(), 1.0):
        raise AssertionError("polygon point weights do not sum to one")
    report = {
        "source_shapefile": str(Path(shapefile).resolve()),
        "source_features": int(len(source)),
        "normalized_class_query": f"{class_column} == {target_value}",
        "balsa_polygons": int(len(balsa)),
        "usable_polygons": int(polygons["n_points"].gt(0).sum()),
        "polygons_inside_11_tiles": int(polygons["inside_11_tiles"].fillna(False).sum()),
        "polygons_outside_11_tiles": int((~polygons["inside_11_tiles"].fillna(False)).sum()),
        "polygons_inside_current_11_s1_grids": int(polygons["inside_11_s1_grids"].fillna(False).sum()),
        "points_all": int(len(points)),
        "points_inside_11_tile_polygons": int(points["inside_11_tiles"].sum()),
        "points_inside_current_11_s1_grid_polygons": int(points["inside_11_s1_grids"].sum()),
        "points_per_polygon": points.groupby("polygon_id").size().describe().to_dict(),
        "polygon_tile_counts": polygons["tile_id"].value_counts().sort_index().to_dict(),
        "analysis_tiles": sorted(tile_set),
        "s1_catalog_sources": catalog_sources,
        "sampling": {
            "hectares_per_point": hectares_per_point,
            "max_points": max_points,
            "boundary_buffer_m": boundary_buffer_m,
            "anchor": "maximum-inscribed polylabel on eroded geometry; representative-point fallback",
            "additional_points": "deterministic farthest-point subset of interior grid",
            "weight": "1/n_points_polygon so every polygon has total weight 1",
        },
    }
    return points, polygons, report


def main() -> None:
    args = parse_args()
    points, polygons, report = build_inventory(
        args.shapefile,
        class_column=args.class_column,
        class_value=args.class_value,
        hectares_per_point=args.hectares_per_point,
        max_points=args.max_points,
        boundary_buffer_m=args.boundary_buffer_m,
        polylabel_tolerance_m=args.polylabel_tolerance_m,
        analysis_tiles=tuple(args.tiles),
        s1_catalog=args.s1_catalog,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    all_points = args.out_dir / "balsa_polygon_aef_points_all.csv"
    tile_points = args.out_dir / "balsa_polygon_aef_points_11tiles.csv"
    polygon_csv = args.out_dir / "balsa_polygon_inventory.csv"
    points.to_csv(all_points, index=False)
    scope_column = "inside_11_s1_grids" if args.s1_catalog else "inside_11_tiles"
    points.loc[points[scope_column]].to_csv(tile_points, index=False)
    polygons.to_csv(polygon_csv, index=False)
    (args.out_dir / "balsa_polygon_sampling_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({**report, "outputs": {
        "points_all": str(all_points), "points_11tiles": str(tile_points),
        "polygons": str(polygon_csv),
    }}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
