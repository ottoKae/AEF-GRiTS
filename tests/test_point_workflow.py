import json
from pathlib import Path
import subprocess
import sys

from affine import Affine
import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, Polygon, box
import zarr

from aef_grits.earth_engine import feature_columns
from aef_grits.point_store import load_aef_points
from aef_grits.points import load_point_source, validate_point_table
from aef_grits.store import AEFCatalog
from scripts.stream_aef_grid_ee import _create_store


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("suffix", "driver", "layer"),
    [
        (".shp", "ESRI Shapefile", None),
        (".gpkg", "GPKG", "samples"),
        (".geojson", "GeoJSON", None),
    ],
)
def test_vector_formats_convert_polygon_to_interior_representative(
    tmp_path, suffix, driver, layer
):
    geometry = Polygon(
        [(-79.30, -1.10), (-79.28, -1.10), (-79.28, -1.08), (-79.30, -1.08)]
    )
    vectors = gpd.GeoDataFrame(
        {"polygon_id": ["p001"], "species": ["BALSA"]},
        geometry=[geometry],
        crs="EPSG:4326",
    )
    path = tmp_path / f"samples{suffix}"
    vectors.to_file(path, driver=driver, layer=layer)
    points = load_point_source(path, layer=layer, id_field="polygon_id")
    assert list(points.sample_id) == ["p001"]
    assert points.loc[0, "species"] == "BALSA"
    assert points.loc[0, "point_method"] == "representative_point"
    assert geometry.contains(Point(points.loc[0, "lon"], points.loc[0, "lat"]))


def test_geoparquet_point_geometry_is_accepted(tmp_path):
    path = tmp_path / "points.parquet"
    gpd.GeoDataFrame(
        {"site": ["one"]}, geometry=[Point(-79.3, -1.1)], crs="EPSG:4326"
    ).to_parquet(path)
    points = load_point_source(path, id_field="site")
    assert list(points[["sample_id", "lon", "lat"]].iloc[0]) == ["one", -79.3, -1.1]


def test_polygon_interior_pixels_follow_reference_grid(tmp_path):
    reference = tmp_path / "reference.tif"
    transform = from_origin(500_000, 10_000_000, 10, 10)
    with rasterio.open(
        reference,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint8",
        crs="EPSG:32717",
        transform=transform,
    ) as target:
        target.write(np.zeros((1, 4, 4), dtype=np.uint8))
    polygon_path = tmp_path / "polygon.geojson"
    gpd.GeoDataFrame(
        {"polygon_id": ["plot"]},
        geometry=[box(500_010, 9_999_970, 500_030, 9_999_990)],
        crs="EPSG:32717",
    ).to_file(polygon_path, driver="GeoJSON")
    points = load_point_source(
        polygon_path,
        id_field="polygon_id",
        geometry_mode="interior_pixels",
        reference_grid=reference,
    )
    assert len(points) == 4
    assert points.sample_id.is_unique
    assert set(points.point_method) == {"interior_pixel"}


def test_point_preflight_reports_duplicates_ranges_and_years():
    frame = pd.DataFrame(
        {
            "sample_id": ["a", "b", "c"],
            "lon": [-79.0, -79.0, 999.0],
            "lat": [-1.0, -1.0, 0.0],
            "split": ["train", "train", "validation"],
        }
    )
    _, report = validate_point_table(frame, [2016, 2025], chunk_size=2)
    assert not report["valid"]
    assert report["estimated_shards"] == 2
    assert report["feature_count"] == 128
    assert report["duplicate_coordinate_rows"] == 2
    assert report["group_counts"]["split"] == {"train": 2, "validation": 1}
    assert any("supported AEF range" in error for error in report["errors"])
    assert any("outside lon" in error for error in report["errors"])


def test_validate_only_does_not_require_earth_engine_project(tmp_path):
    points = tmp_path / "points.csv"
    pd.DataFrame(
        {"sample_id": ["a"], "lon": [-79.3], "lat": [-1.1]}
    ).to_csv(points, index=False)
    output = tmp_path / "validation"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "stream_aef_points_ee.py"),
            "--samples",
            str(points),
            "--out-dir",
            str(output),
            "--years",
            "2025",
            "--validate-only",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "validation_report.json").read_text(encoding="utf-8"))
    assert report["valid"]
    assert report["estimated_shards"] == 1


def test_unified_point_loader_checks_schema_and_completeness(tmp_path):
    columns = feature_columns([2025])
    frames = []
    for shard_id in (1, 2):
        frame = pd.DataFrame(
            {"sample_id": [f"p{shard_id}"], "lon": [-79.0], "lat": [-1.0]}
        )
        for column in columns:
            frame[column] = np.float32(shard_id)
        if shard_id == 2:
            frame.loc[0, columns[0]] = np.nan
        path = tmp_path / f"part{shard_id:05d}.parquet"
        frame.to_parquet(path, index=False)
        frames.append(
            {
                "chunk": shard_id,
                "path": str(path),
                "rows": 1,
                "complete_rows": int(shard_id == 1),
                "signature": "same",
            }
        )
    catalog = tmp_path / "catalog.parquet"
    pd.DataFrame(frames).to_parquet(catalog, index=False)
    dataset = load_aef_points(catalog, years=[2025])
    assert len(dataset.frame) == 2
    assert dataset.report["complete_rows"] == 1
    with pytest.raises(ValueError, match="incomplete"):
        load_aef_points(catalog, years=[2025], require_complete=True)
    with pytest.raises(ValueError, match="absent"):
        load_aef_points(catalog, years=[2024])


def _synthetic_grid(grid_id, scheme, x0):
    return {
        "crs": "EPSG:32717",
        "transform": Affine(10, 0, x0, 0, -10, 10_000_000),
        "width": 8,
        "height": 8,
        "bounds": (x0, 9_999_920, x0 + 80, 10_000_000),
        "tile_id": grid_id,
        "grid_id": grid_id,
        "scheme": scheme,
    }


def test_tessera_and_mgrs_catalog_patch_delivery(tmp_path):
    rows = []
    point_rows = []
    transformer = Transformer.from_crs("EPSG:32717", "EPSG:4326", always_xy=True)
    specifications = [
        ("grid_-79.95_-1.05", "tessera_0p1", 500_000, 1.0),
        ("17MPU", "mgrs", 501_000, 2.0),
    ]
    for grid_id, scheme, x0, fill in specifications:
        path = tmp_path / f"{grid_id}.zarr"
        group = _create_store(path, _synthetic_grid(grid_id, scheme, x0), [2025], "sig", 4, 8)
        group["embeddings"][0] = fill
        zarr.consolidate_metadata(str(path))
        rows.append(
            {
                "scheme": scheme,
                "grid_id": grid_id,
                "tile_id": grid_id,
                "zarr_path": str(path),
                "status": "complete",
            }
        )
        x, y = x0 + 45, 10_000_000 - 45
        lon, lat = transformer.transform(x, y)
        point_rows.append(
            {"sample_id": grid_id, "lon": lon, "lat": lat, "grid_id": grid_id}
        )
    catalog_path = tmp_path / "catalog.parquet"
    pd.DataFrame(rows).to_parquet(catalog_path, index=False)
    catalog = AEFCatalog(catalog_path)
    patches = catalog.sample_patches(
        [(row["lon"], row["lat"]) for row in point_rows],
        years=[2025],
        patch_size=3,
        grid_ids=[row["grid_id"] for row in point_rows],
    )
    assert patches.shape == (2, 1, 64, 3, 3)
    assert np.all(patches[0] == 1.0)
    assert np.all(patches[1] == 2.0)

    samples = tmp_path / "patch_points.parquet"
    pd.DataFrame(point_rows).to_parquet(samples, index=False)
    output = tmp_path / "patch_output"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "extract_aef_patches.py"),
            "--samples",
            str(samples),
            "--catalog",
            str(catalog_path),
            "--out-dir",
            str(output),
            "--years",
            "2025",
            "--patch-size",
            "3",
            "--batch-size",
            "1",
            "--require-complete",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["complete_rows"] == 2
    tensors = sorted((output / "shards").glob("*.npy"))
    assert len(tensors) == 2
    assert np.load(tensors[0]).shape == (1, 1, 64, 3, 3)
