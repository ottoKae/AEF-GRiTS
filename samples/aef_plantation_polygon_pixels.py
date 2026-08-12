#!/usr/bin/env python
"""Build deterministic, polygon-equal AEF samples for every plantation polygon."""

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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from samples.aef_balsa_polygon_pixels import (  # noqa: E402
    DEFAULT_11_TILES,
    _anchor_point,
    _candidate_grid,
    _farthest_points,
    _mgrs_tile,
    _repair_geometry,
)
from aef_grits.catalog import choose_grid, load_catalog_grids  # noqa: E402


DEFAULT_OUT_DIR = PROJECT_ROOT / "outputs/plantation_inventory"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shapefile", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--common-name-column", default="nco")
    parser.add_argument("--scientific-name-column", default="csp")
    parser.add_argument("--hectares-per-point", type=float, default=0.25)
    parser.add_argument("--max-points", type=int, default=25)
    parser.add_argument("--boundary-buffer-m", type=float, default=10.0)
    parser.add_argument("--polylabel-tolerance-m", type=float, default=1.0)
    parser.add_argument("--tiles", nargs="+", default=list(DEFAULT_11_TILES))
    parser.add_argument(
        "--grid-catalog",
        "--s1-catalog",
        dest="s1_catalog",
        type=Path,
        help=(
            "Optional local raster catalog for exact point-to-grid assignment; "
            "--s1-catalog is retained as a compatibility alias"
        ),
    )
    return parser.parse_args()


def _normalized(value: object) -> str:
    if value is None or pd.isna(value):
        return "<MISSING>"
    text = str(value).strip().upper()
    return text or "<MISSING>"


def build_inventory(
    shapefile: str | Path,
    *,
    common_name_column: str = "nco",
    scientific_name_column: str = "csp",
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
    missing = {common_name_column, scientific_name_column} - set(source.columns)
    if missing:
        raise ValueError(f"missing species columns: {sorted(missing)}")

    metric = source.to_crs("EPSG:32717")
    to_wgs84 = Transformer.from_crs("EPSG:32717", "EPSG:4326", always_xy=True)
    mgrs_encoder = MGRS()
    tile_set = {str(value).upper() for value in analysis_tiles}
    grids, catalog_sources = (load_catalog_grids(s1_catalog) if s1_catalog else ([], []))
    point_records: list[dict[str, object]] = []
    polygon_records: list[dict[str, object]] = []

    for position, (source_index, source_row) in enumerate(source.iterrows()):
        geometry, repair_status = _repair_geometry(metric.iloc[position].geometry)
        polygon_id = f"plantation_{source_index}"
        common_name = _normalized(source_row.get(common_name_column))
        scientific_name = _normalized(source_row.get(scientific_name_column))
        if geometry is None or geometry.is_empty:
            polygon_records.append(
                {
                    "polygon_id": polygon_id,
                    "source_index": source_index,
                    "species_nco": common_name,
                    "species_csp": scientific_name,
                    "geometry_repair_status": repair_status,
                    "n_points": 0,
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
        points = _farthest_points(
            _candidate_grid(sampling_geometry, target_points), anchor, target_points
        )
        lon, lat = to_wgs84.transform(
            np.array([point.x for point in points]),
            np.array([point.y for point in points]),
        )
        point_tiles = [
            _mgrs_tile(mgrs_encoder, latitude, longitude)
            for longitude, latitude in zip(lon, lat)
        ]
        polygon_tile = point_tiles[0]
        s1_grids = [choose_grid(float(point.x), float(point.y), grids) for point in points]
        point_s1_tiles = [grid["tile_id"] if grid is not None else None for grid in s1_grids]
        anchor_s1_tile = point_s1_tiles[0]
        attributes = {
            "species_nco": common_name,
            "species_csp": scientific_name,
            "source_objectid": source_row.get("OBJECTID"),
            "source_nco": source_row.get(common_name_column),
            "source_csp": source_row.get(scientific_name_column),
            "source_sce_ha": source_row.get("sce"),
            "province": source_row.get("DPA_DESPRO"),
            "canton": source_row.get("DPA_DESCAN"),
            "parish": source_row.get("DPA_DESPAR"),
        }
        n_points = len(points)
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
                    "inside_11_s1_grids": anchor_s1_tile is not None,
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
                "inside_11_s1_grids": anchor_s1_tile is not None,
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
        "usable_polygons": int(polygons["n_points"].gt(0).sum()),
        "common_name_classes": int(polygons["species_nco"].nunique()),
        "scientific_name_classes": int(polygons["species_csp"].nunique()),
        "common_name_counts": polygons["species_nco"].value_counts().sort_index().to_dict(),
        "scientific_name_counts": polygons["species_csp"].value_counts().sort_index().to_dict(),
        "polygons_inside_11_tiles": int(polygons["inside_11_tiles"].fillna(False).sum()),
        "polygons_outside_11_tiles": int((~polygons["inside_11_tiles"].fillna(False)).sum()),
        "points_all": int(len(points)),
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
        common_name_column=args.common_name_column,
        scientific_name_column=args.scientific_name_column,
        hectares_per_point=args.hectares_per_point,
        max_points=args.max_points,
        boundary_buffer_m=args.boundary_buffer_m,
        polylabel_tolerance_m=args.polylabel_tolerance_m,
        analysis_tiles=tuple(args.tiles),
        s1_catalog=args.s1_catalog,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    point_path = args.out_dir / "plantation_polygon_aef_points_all.csv"
    polygon_path = args.out_dir / "plantation_polygon_inventory.csv"
    points.to_csv(point_path, index=False)
    polygons.to_csv(polygon_path, index=False)
    report_path = args.out_dir / "plantation_polygon_sampling_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({**report, "outputs": {
        "points_all": str(point_path), "polygons": str(polygon_path), "report": str(report_path)
    }}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
