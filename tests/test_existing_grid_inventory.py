import json
import os

from affine import Affine
import numpy as np
import pandas as pd
import pytest
import rasterio
import zarr

from aef_grits.grids import MGRSGridProvider
from tooling.diagnostics.existing_grids_20261101.inventory import collect_inventory, main


def make_store(path, *, full=False):
    grid = MGRSGridProvider().get("50RMU")
    width, height = (grid.width, grid.height) if full else (3, 2)
    group = zarr.open_group(str(path), mode="w")
    group.create_array("embeddings", shape=(2, 64, height, width), chunks=(1, 64, 2, 3), dtype="float32")
    group.create_array("time", data=np.array([2020, 2025], dtype=np.int32))
    group.attrs.update({
        "crs": grid.crs, "transform": list(grid.transform)[:6], "width": width,
        "height": height, "grid_id": "50RMU" if full else "50RMU_pilot",
        "aef:grid_scheme": "mgrs" if full else "reference",
    })


def make_tiff(path, *, count=64, tags=None):
    with rasterio.open(path, "w", driver="GTiff", width=3, height=2, count=count,
                       dtype="float32", crs="EPSG:32650", transform=Affine(30, 0, 400000, 0, -30, 3400000)) as target:
        target.write(np.ones((count, 2, 3), dtype=np.float32))
        if tags:
            target.update_tags(**tags)


def test_inventory_actual_years_footprints_and_read_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    make_store(source / "aef_50RMU_2020_2025.zarr", full=True)
    make_store(source / "aef_50RMU_pilot_2020_2025.zarr")
    make_tiff(source / "aef_50RMU_2023_30m.tif")
    make_tiff(source / "unknown_2020_2025.tif")
    make_tiff(source / "ancillary.tif", count=1)
    (source / "catalog.parquet").write_bytes(b"not a point-table read")
    (source / "geotiff_manifest.json").write_text("{}")
    before = {str(path): path.stat().st_mtime_ns for path in source.rglob("*")}
    out = tmp_path / "report"
    assert main(["--roots", str(source), "--out", str(out)]) == 0
    frame = pd.read_parquet(out / "inventory.parquet")
    discovery = json.loads((out / "discovery.json").read_text())
    assert len(frame) == 6
    assert set(frame.loc[frame.format == "zarr", "year"]) == {2020, 2025}
    assert set(frame.loc[frame.grid_id == "50RMU", "footprint_class"]) == {"full_mgrs"}
    subset = frame.loc[frame.grid_id == "50RMU_pilot"]
    assert set(subset.footprint_class) == {"mgrs_subset/reference"}
    assert set(subset.mgrs_candidate_evidence) == {"grid_id:derived_name"}
    derivative = frame.loc[frame.path.str.endswith("30m.tif")].iloc[0]
    assert derivative.year == 2023
    assert derivative.resolution_class == "30m_derivative_not_native_10m"
    assert derivative.protocol == "unknown"
    assert not derivative.source_manifest_presence
    assert pd.isna(frame.loc[frame.path.str.contains("unknown_"), "year"].iloc[0])
    assert len(discovery["catalogs"]) == len(discovery["geotiff_manifests"]) == len(discovery["auxiliary"]) == 1
    assert any(error["stage"] == "year" for error in discovery["errors"])
    assert before == {str(path): path.stat().st_mtime_ns for path in source.rglob("*")}
    with pytest.raises(SystemExit):
        main(["--roots", str(source), "--out", str(out)])


def test_aliases_missing_roots_errors_and_pruning(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    make_tiff(source / "2025.tif", tags={"year": "2024", "native_protocol": "native-utm-v1", "native_source_manifest": '{"signature":"test"}'})
    os.link(source / "2025.tif", source / "alias.tif")
    for name in (".git", ".aef_resources", "chunks.zarr", "unreadable"):
        (source / name).mkdir()
        make_tiff(source / name / "2025.tif")
    original = os.scandir

    def scandir(path):
        if str(path).endswith("unreadable"):
            raise PermissionError("test unreadable directory")
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    frame, discovery = collect_inventory([source, source, tmp_path / "missing"])
    assert len(frame) == 1
    assert frame.iloc[0].year == 2024
    assert frame.iloc[0].year_evidence == "tiff_year_tag"
    assert frame.iloc[0].source_manifest_presence
    assert frame.iloc[0].protocol == "native-utm-v1"
    assert len(discovery["aliases"]) == 2
    assert len(discovery["pruned"]) == 2
    assert [root["status"] for root in discovery["roots"]] == ["scan_errors", "alias_already_scanned", "missing"]
    assert {error["stage"] for error in discovery["errors"]} == {"discovery", "raster_metadata"}


@pytest.mark.parametrize("transform", [Affine(10, 0, 400000, 0, 10, 3400000), Affine(10, 2, 400000, 3, -10, 3400000)])
def test_positive_y_and_rotated_bounds(tmp_path, transform):
    path = tmp_path / "2025.tif"
    make_tiff(path)
    with rasterio.open(path, "r+") as target:
        target.transform = transform
    frame, discovery = collect_inventory([tmp_path])
    assert len(frame) == 1
    west, south, east, north = json.loads(frame.iloc[0].spatial_bounds_WGS84)
    assert west < east and south < north
    assert not discovery["errors"]
