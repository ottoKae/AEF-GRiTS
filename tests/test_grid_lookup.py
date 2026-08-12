from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, box

from aef_grits.grid_lookup import (
    build_regions,
    build_search_catalog,
    read_aoi,
    resolve_mgrs,
    resolve_tessera,
)


def test_mgrs_lookup_excludes_boundary_only_contacts(tmp_path: Path):
    index = pd.DataFrame(
        {
            "mgrs_tile_id": ["00AAA", "00AAB"],
            "utm_epsg": [32631, 32631],
            "utm_wkt": ["POLYGON EMPTY", "POLYGON EMPTY"],
            "geometry": [box(0, 0, 1, 1).wkb, box(1, 0, 2, 1).wkb],
        }
    )
    index_path = tmp_path / "mgrs.parquet"
    index.to_parquet(index_path, index=False)
    regions = build_regions(
        gpd.GeoDataFrame(
            {"code": ["west"], "cn": ["西部"], "en": ["West"]},
            geometry=[box(0.2, 0.2, 1.0, 0.8)],
            crs=4326,
        ),
        region_id_field="code",
        name_field_cn="cn",
        name_field_en="en",
    )

    rows = resolve_mgrs(regions, index_path)
    assert [row["grid_id"] for row in rows] == ["00AAA"]
    touching = resolve_mgrs(regions, index_path, include_touching=True)
    assert [row["grid_id"] for row in touching] == ["00AAA", "00AAB"]


def test_tessera_polygon_and_point_have_deterministic_cells():
    polygon = build_regions(
        gpd.GeoDataFrame(
            {"id": ["p"]},
            geometry=[box(-79.20, -0.20, -79.01, -0.01)],
            crs=4326,
        ),
        region_id_field="id",
    )
    polygon_ids = {row["grid_id"] for row in resolve_tessera(polygon)}
    assert polygon_ids == {
        "grid_-79.15_-0.15",
        "grid_-79.15_-0.05",
        "grid_-79.05_-0.15",
        "grid_-79.05_-0.05",
    }

    point = build_regions(
        gpd.GeoDataFrame({"id": ["q"]}, geometry=[Point(-79.1, -0.1)], crs=4326),
        region_id_field="id",
    )
    point_rows = resolve_tessera(point)
    assert [row["grid_id"] for row in point_rows] == ["grid_-79.05_-0.05"]


def test_bilingual_catalog_is_searchable(tmp_path: Path):
    source = tmp_path / "province.geojson"
    gpd.GeoDataFrame(
        {"code": ["EC-SD"], "cn": ["圣多明各"], "en": ["Santo Domingo"]},
        geometry=[box(-79.2, -0.2, -79.1, -0.1)],
        crs=4326,
    ).to_file(source, driver="GeoJSON")
    regions = build_regions(
        read_aoi(source),
        region_id_field="code",
        name_field_cn="cn",
        name_field_en="en",
    )
    catalog = build_search_catalog(pd.DataFrame(resolve_tessera(regions)))
    assert len(catalog) == 1
    text = catalog.iloc[0].search_text
    assert "圣多明各" in text
    assert "santo domingo" in text
    assert "ec-sd" in text
