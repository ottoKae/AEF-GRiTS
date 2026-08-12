"""Resolve vector areas or points to MGRS and Tessera AEF grid identifiers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely import from_wkb, make_valid, union_all, wkt
from shapely.geometry import box
from shapely.ops import transform

from .grids import tessera_grid_name, tessera_tile_from_world, utm_epsg_for_lonlat


VECTOR_SUFFIXES = {".shp", ".gpkg", ".geojson", ".json", ".parquet"}


def read_aoi(path: str | Path, *, layer: str | None = None) -> gpd.GeoDataFrame:
    """Read a vector AOI, validate its declared CRS and return WGS84 geometry."""
    source = Path(path)
    if source.suffix.lower() not in VECTOR_SUFFIXES:
        raise ValueError(
            "AOI must be Shapefile, GeoPackage, GeoJSON or GeoParquet"
        )
    frame = (
        gpd.read_parquet(source)
        if source.suffix.lower() == ".parquet"
        else gpd.read_file(source, layer=layer)
    )
    if frame.empty:
        raise ValueError(f"AOI is empty: {source}")
    if frame.crs is None:
        raise ValueError(f"AOI has no declared CRS: {source}")
    frame = frame.to_crs("EPSG:4326").copy()
    frame.geometry = frame.geometry.map(
        lambda geometry: make_valid(geometry)
        if geometry is not None and not geometry.is_empty
        else geometry
    )
    frame = frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    if frame.empty:
        raise ValueError(f"AOI has no usable geometry: {source}")
    return frame


def _joined_values(frame: pd.DataFrame, field: str | None) -> str:
    if not field:
        return ""
    if field not in frame:
        raise ValueError(f"AOI field is absent: {field}")
    values = sorted(
        {
            str(value).strip()
            for value in frame[field].dropna()
            if str(value).strip()
        }
    )
    return "|".join(values)


def build_regions(
    aoi: gpd.GeoDataFrame,
    *,
    region_id_field: str | None = None,
    name_field_cn: str | None = None,
    name_field_en: str | None = None,
    default_region_id: str = "aoi",
) -> list[dict]:
    """Dissolve AOI features into named regions used by the lookup tables."""
    for field in (region_id_field, name_field_cn, name_field_en):
        if field and field not in aoi:
            raise ValueError(f"AOI field is absent: {field}")
    groups = (
        [(str(key), group) for key, group in aoi.groupby(region_id_field, dropna=False)]
        if region_id_field
        else [(default_region_id, aoi)]
    )
    regions = []
    for region_id, group in groups:
        geometry = make_valid(union_all(group.geometry.to_numpy()))
        if geometry.is_empty:
            continue
        regions.append(
            {
                "region_id": region_id,
                "region_name_cn": _joined_values(group, name_field_cn),
                "region_name_en": _joined_values(group, name_field_en),
                "feature_count": int(len(group)),
                "geometry": geometry,
            }
        )
    if not regions:
        raise ValueError("No non-empty regions could be built from AOI")
    return regions


def _has_positive_intersection(source, candidate, include_touching: bool) -> bool:
    intersection = source.intersection(candidate)
    if intersection.is_empty:
        return False
    if include_touching:
        return True
    if source.area > 0:
        return intersection.area > 0
    if source.length > 0:
        return intersection.length > 0
    return True


def load_mgrs_geographic_index(path: str | Path) -> gpd.GeoDataFrame:
    """Load the authoritative S1-GRiTS MGRS table as WGS84 geometries."""
    source = Path(path)
    frame = pd.read_parquet(source)
    required = {"mgrs_tile_id", "utm_epsg", "utm_wkt"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"MGRS table missing columns: {sorted(missing)}")
    if "geometry" in frame:
        geometries = from_wkb(frame.geometry.to_numpy())
    else:
        geometries = []
        for row in frame.itertuples(index=False):
            projected = wkt.loads(str(row.utm_wkt))
            transformer = Transformer.from_crs(
                f"EPSG:{int(row.utm_epsg)}", "EPSG:4326", always_xy=True
            )
            geometries.append(transform(transformer.transform, projected))
    attributes = frame.drop(columns=["geometry"], errors="ignore").copy()
    return gpd.GeoDataFrame(attributes, geometry=geometries, crs="EPSG:4326")


def resolve_mgrs(
    regions: Sequence[dict],
    mgrs_index: str | Path,
    *,
    include_touching: bool = False,
) -> list[dict]:
    """Return region-to-MGRS intersections using authoritative geometries."""
    grids = load_mgrs_geographic_index(mgrs_index)
    rows = []
    for region in regions:
        indices = grids.sindex.query(region["geometry"], predicate="intersects")
        for index in sorted(map(int, indices)):
            grid = grids.iloc[index]
            if not _has_positive_intersection(
                region["geometry"], grid.geometry, include_touching
            ):
                continue
            rows.append(
                {
                    "region_id": region["region_id"],
                    "region_name_cn": region["region_name_cn"],
                    "region_name_en": region["region_name_en"],
                    "scheme": "mgrs",
                    "grid_id": str(grid.mgrs_tile_id),
                    "utm_epsg": int(grid.utm_epsg),
                    "tile_center_lon": float(grid.geometry.centroid.x),
                    "tile_center_lat": float(grid.geometry.centroid.y),
                    "geographic_wkt": grid.geometry.wkt,
                    "geometry": grid.geometry.wkb,
                    "utm_wkt": str(grid.utm_wkt),
                }
            )
    return rows


def _tessera_cells_for_geometry(geometry) -> list[tuple[float, float]]:
    # Points have an exact half-open containing-cell convention. This prevents a
    # point on a 0.1-degree boundary from being assigned to four touching cells.
    if geometry.area == 0 and geometry.length == 0:
        from shapely import get_coordinates

        return sorted(
            {
                tessera_tile_from_world(float(lon), float(lat))
                for lon, lat in get_coordinates(geometry)
            }
        )
    west, south, east, north = geometry.bounds
    lon_start, lon_stop = math.floor(west * 10.0), math.ceil(east * 10.0)
    lat_start, lat_stop = math.floor(south * 10.0), math.ceil(north * 10.0)
    return [
        (round(lon_index / 10.0 + 0.05, 2), round(lat_index / 10.0 + 0.05, 2))
        for lon_index in range(lon_start, lon_stop)
        for lat_index in range(lat_start, lat_stop)
    ]


def resolve_tessera(
    regions: Sequence[dict], *, include_touching: bool = False
) -> list[dict]:
    """Return region-to-Tessera-0.1-degree intersections."""
    rows = []
    for region in regions:
        for lon, lat in _tessera_cells_for_geometry(region["geometry"]):
            west, east = lon - 0.05, lon + 0.05
            south, north = lat - 0.05, lat + 0.05
            geometry = box(west, south, east, north)
            if not _has_positive_intersection(
                region["geometry"], geometry, include_touching
            ):
                continue
            rows.append(
                {
                    "region_id": region["region_id"],
                    "region_name_cn": region["region_name_cn"],
                    "region_name_en": region["region_name_en"],
                    "scheme": "tessera_0p1",
                    "grid_id": tessera_grid_name(lon, lat),
                    "utm_epsg": utm_epsg_for_lonlat(lon, lat),
                    "tile_center_lon": lon,
                    "tile_center_lat": lat,
                    "geographic_wkt": geometry.wkt,
                    "geometry": geometry.wkb,
                    "utm_wkt": "",
                }
            )
    return rows


def build_search_catalog(intersections: pd.DataFrame) -> pd.DataFrame:
    """Collapse region intersections into one bilingual searchable row per grid."""
    if intersections.empty:
        return pd.DataFrame()
    records = []
    for (scheme, grid_id), group in intersections.groupby(
        ["scheme", "grid_id"], sort=True
    ):
        first = group.iloc[0]
        region_ids = "|".join(sorted(set(group.region_id.astype(str))))
        names_cn = "|".join(
            sorted({value for value in group.region_name_cn.astype(str) if value})
        )
        names_en = "|".join(
            sorted({value for value in group.region_name_en.astype(str) if value})
        )
        records.append(
            {
                "scheme": scheme,
                "grid_id": grid_id,
                "utm_epsg": int(first.utm_epsg),
                "tile_center_lon": float(first.tile_center_lon),
                "tile_center_lat": float(first.tile_center_lat),
                "region_ids": region_ids,
                "region_names_cn": names_cn,
                "region_names_en": names_en,
                "search_text": " ".join(
                    [str(grid_id), region_ids, names_cn, names_en]
                ).casefold(),
                "geographic_wkt": first.geographic_wkt,
                "geometry": first.geometry,
                "utm_wkt": first.utm_wkt,
            }
        )
    return pd.DataFrame.from_records(records)
