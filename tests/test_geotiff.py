import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine
import zarr

from aef_grits import geotiff
from aef_grits.geotiff import export_catalog, export_geotiffs, validate_geotiffs


def make_store(path: Path):
    data = np.arange(2 * 64 * 17 * 19, dtype=np.float32).reshape(2, 64, 17, 19) / 100000
    data[:, :, 2, 3] = np.nan
    group = zarr.open_group(str(path), mode="w")
    group.create_array("embeddings", data=data, chunks=(1, 64, 8, 8))
    group.create_array("time", data=np.array([2024, 2025]))
    group.attrs.update({"crs": "EPSG:32717", "transform": [10, 0, 400000, 0, -10, 9900000],
                        "width": 19, "height": 17, "grid_id": "test", "aef:signature": "test-v1"})
    return data


def test_export_preserves_values_coordinates_years_and_resumes(tmp_path):
    source = tmp_path / "test.zarr"
    expected = make_store(source)
    records = export_geotiffs(source, tmp_path / "tiff")
    times = []
    for index, record in enumerate(records):
        path = Path(record["path"])
        times.append(path.stat().st_mtime_ns)
        with rasterio.open(path) as dataset:
            np.testing.assert_array_equal(dataset.read(), expected[index])
            assert dataset.crs.to_epsg() == 32717
            assert dataset.transform == Affine(10, 0, 400000, 0, -10, 9900000)
            assert dataset.descriptions == tuple(f"A{i:02d}" for i in range(64))
            assert dataset.tags()["year"] == str(2024 + index)
            assert np.isnan(dataset.nodata)
    again = export_geotiffs(source, tmp_path / "tiff")
    assert times == [Path(row["path"]).stat().st_mtime_ns for row in again]
    assert source.exists()


def test_export_does_not_replace_unrelated_files(tmp_path):
    source = tmp_path / "test.zarr"
    make_store(source)
    records = export_geotiffs(source, tmp_path / "tiff")
    with rasterio.open(records[0]["path"], "r+") as existing:
        existing.update_tags(aef_export_signature="different")
    with pytest.raises(FileExistsError, match="Refusing"):
        export_geotiffs(source, tmp_path / "tiff")


def test_export_reads_bounded_windows_and_preserves_edge_values(tmp_path, monkeypatch):
    from scripts.stream_aef_grid_ee import _create_store

    source = tmp_path / "wide.zarr"
    grid = {"width": 263, "height": 259, "crs": "EPSG:32650", "grid_id": "wide",
            "transform": Affine(10, 0, 400000, 0, -10, 3300000)}
    group = _create_store(source, grid, [2025], "wide-v1", 64, 512)
    expected = np.arange(64 * 259 * 263, dtype=np.float32).reshape(64, 259, 263) / 1e7
    expected[:, 256, 258] = np.nan
    group["embeddings"][0] = expected
    original = zarr.Array.__getitem__
    reads = []

    def checked_read(array, selection):
        if array.path == "embeddings":
            reads.append(selection)
            assert selection[2].stop - selection[2].start <= 256
            assert selection[3].stop - selection[3].start <= 256
        return original(array, selection)

    monkeypatch.setattr(zarr.Array, "__getitem__", checked_read)
    records = export_geotiffs(source, tmp_path / "tiff")
    assert len(reads) == 4
    with rasterio.open(records[0]["path"]) as dataset:
        np.testing.assert_array_equal(dataset.read(), expected)
        assert dataset.crs.to_epsg() == 32650
        assert np.all(dataset.read_masks()[:, 256, 258] == 0)
        assert np.all(dataset.read_masks()[:, 0, 0] == 255)


def test_interrupted_export_leaves_source_and_no_partial_final(tmp_path, monkeypatch):
    source = tmp_path / "test.zarr"
    expected = make_store(source)
    destination = tmp_path / "tiff"
    original = geotiff.os.replace

    def interrupted_commit(*args):
        raise OSError("simulated interrupted atomic commit")

    monkeypatch.setattr(geotiff.os, "replace", interrupted_commit)
    with pytest.raises(OSError, match="interrupted"):
        export_geotiffs(source, destination)
    assert list(destination.iterdir()) == []
    np.testing.assert_array_equal(zarr.open_group(source, mode="r")["embeddings"][:], expected)
    monkeypatch.setattr(geotiff.os, "replace", original)
    assert len(export_geotiffs(source, destination)) == 2


def test_resume_rejects_wrong_georeferencing_without_overwrite(tmp_path):
    source = tmp_path / "test.zarr"
    make_store(source)
    records = export_geotiffs(source, tmp_path / "tiff")
    target = Path(records[0]["path"])
    with rasterio.open(target, "r+") as dataset:
        dataset.transform = Affine(10, 0, 400010, 0, -10, 9900000)
    altered_time = target.stat().st_mtime_ns
    with pytest.raises(FileExistsError, match="Refusing"):
        export_geotiffs(source, tmp_path / "tiff")
    assert target.stat().st_mtime_ns == altered_time


def test_catalog_export_and_delivery_validation_native_zarr(tmp_path):
    from aef_grits.delivery_validation import validate_delivery
    from scripts.stream_aef_grid_ee import _create_store

    output = tmp_path / "output"
    control = tmp_path / "state"
    control.mkdir()
    source = output / "mgrs" / "native.zarr"
    grid = {"width": 19, "height": 17, "crs": "EPSG:32717", "grid_id": "native",
            "transform": Affine(10, 0, 400000, 0, -10, 9900000)}
    group = _create_store(source, grid, [2025], "native-v1", 64, 512)
    expected = np.full((64, 17, 19), 0.125, dtype=np.float32)
    expected[:, 3, 4] = np.nan
    group["embeddings"][0] = expected
    zarr.consolidate_metadata(source)
    catalog_path = control / "catalog.parquet"
    pd.DataFrame([{
        "grid_id": "native", "status": "complete", "zarr_path": str(source.resolve()),
        "crs": grid["crs"], "resolution_m": 10, "compression_codec": "zstd",
        "compression_level": 7, "compression_shuffle": "noshuffle",
    }]).to_parquet(catalog_path, index=False)
    (control / "run_summary.json").write_text(json.dumps({"completed": 1, "failed": []}), encoding="utf-8")
    report = export_catalog(catalog_path, output)
    manifest = json.loads((output / "geotiff_manifest.json").read_text(encoding="utf-8"))
    assert report == manifest
    assert validate_geotiffs(output, ["native"], [2025]) == []
    result = validate_delivery({
        "output_dir": str(output), "control_dir": str(control),
        "params": {"workflow": "grid", "grid_ids": ["native"], "years": [2025], "output_format": "zarr+geotiff"},
    })
    assert result["valid"], result["errors"]
    raster = Path(report["grids"][0]["files"][0]["path"])
    with rasterio.open(raster) as dataset:
        np.testing.assert_array_equal(dataset.read(), expected)
    with rasterio.open(raster, "r+") as dataset:
        dataset.set_band_description(1, "wrong-band")
    assert validate_geotiffs(output, ["native"], [2025])


def test_manifest_rejects_truncated_file_and_incomplete_catalog(tmp_path):
    source = tmp_path / "test.zarr"
    make_store(source)
    catalog = tmp_path / "catalog.parquet"
    pd.DataFrame([{"grid_id": "test", "status": "partial", "zarr_path": str(source)}]).to_parquet(catalog)
    with pytest.raises(ValueError, match="completed"):
        export_catalog(catalog, tmp_path)
    assert not (tmp_path / "geotiff_manifest.json").exists()
    pd.DataFrame([{"grid_id": "test", "status": "complete", "zarr_path": str(source)}]).to_parquet(catalog)
    report = export_catalog(catalog, tmp_path)
    record = report["grids"][0]["files"][0]
    with Path(record["path"]).open("r+b") as file:
        file.truncate(record["bytes"] // 2)
    assert validate_geotiffs(tmp_path, ["test"], [2024, 2025])


def test_large_raster_requests_bigtiff_before_allocating_pixels(tmp_path, monkeypatch):
    from scripts.stream_aef_grid_ee import _create_store

    source = tmp_path / "large.zarr"
    grid = {"width": 5000, "height": 5000, "crs": "EPSG:32650", "grid_id": "large",
            "transform": Affine(10, 0, 400000, 0, -10, 3300000)}
    _create_store(source, grid, [2025], "large-v1", 64, 512)

    def check_create(path, mode, **kwargs):
        assert mode == "w"
        assert kwargs["BIGTIFF"] == "YES"
        assert kwargs["dtype"] == "float32" and kwargs["count"] == 64
        raise RuntimeError("stop before creating a large raster")

    monkeypatch.setattr(geotiff.rasterio, "open", check_create)
    with pytest.raises(RuntimeError, match="before creating"):
        export_geotiffs(source, tmp_path / "tiff")
    assert list((tmp_path / "tiff").iterdir()) == []


def test_catalog_selection_skips_unrelated_historical_store(tmp_path):
    output = tmp_path / "current-task"
    requested = output / "test.zarr"
    historical = tmp_path / "old-task" / "old.zarr"
    make_store(requested)
    make_store(historical)
    zarr.open_group(historical, mode="r+").attrs["grid_id"] = "historical"
    before = {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
              for path in historical.rglob("*") if path.is_file()}
    catalog = tmp_path / "shared-catalog.parquet"
    pd.DataFrame([
        {"grid_id": "test", "status": "complete", "zarr_path": str(requested)},
        {"grid_id": "historical", "status": "partial", "zarr_path": str(historical)},
    ]).to_parquet(catalog, index=False)
    report = export_catalog(catalog, output, grid_ids=["test"])
    assert [row["grid_id"] for row in report["grids"]] == ["test"]
    assert validate_geotiffs(output, ["test"], [2024, 2025]) == []
    assert not historical.with_suffix(".geotiff").exists()
    after = {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
             for path in historical.rglob("*") if path.is_file()}
    assert before == after
