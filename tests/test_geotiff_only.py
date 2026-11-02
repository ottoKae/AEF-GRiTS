import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine

from aef_grits import geotiff
from aef_grits.delivery_validation import validate_delivery
import scripts.stream_aef_grid_ee as stream


@pytest.fixture
def tiny_download(tmp_path, monkeypatch, native_grid_sources):
    reference = tmp_path / "reference.tif"
    with rasterio.open(reference, "w", driver="GTiff", width=17, height=19,
                       count=1, dtype="uint8", crs="EPSG:32717",
                       transform=Affine(10, 0, 500000, 0, -10, 10000000)) as raster:
        raster.write(np.ones((1, 19, 17), dtype=np.uint8))
    output, state = tmp_path / "output", tmp_path / "state"
    argv = ["aef-grits-grid", "--reference", str(reference), "--tile-id", "TEST",
            "--out-dir", str(output), "--state-dir", str(state),
            "--resource-state-dir", str(tmp_path / "resources"),
            "--years", "2024", "2025", "--geotiff-only", "--workers", "1",
            "--block-size", "8", "--inner-chunk", "4", "--shard-size", "8", "--max-retries", "0"]
    monkeypatch.setattr(sys, "argv", argv)
    calls = []

    class Image:
        def __init__(self, year):
            self.year = year

        def unmask(self, *args, **kwargs):
            return self

    def compute_pixels(request):
        calls.append(request["expression"].year)
        dims = request["grid"]["dimensions"]
        return {band: np.full((dims["height"], dims["width"]),
                             index + request["expression"].year / 10000, dtype=np.float32)
                for index, band in enumerate(stream.AEF_BANDS)}

    ee = SimpleNamespace(Geometry=SimpleNamespace(Rectangle=lambda *args, **kwargs: object()),
                         data=SimpleNamespace(computePixels=compute_pixels))
    monkeypatch.setattr(stream, "initialize", lambda *args, **kwargs: (
        ee, SimpleNamespace(public_summary=lambda: {"source": "test"})))
    native_grid_sources(ee, {"crs": "EPSG:32717", "transform": Affine(10, 0, 500000, 0, -10, 10000000),
                            "width": 17, "height": 19}, [2024, 2025], Image)
    return output, state, calls


def test_tiff_only_cli_delivers_without_zarr_and_resumes_without_network(tiny_download, monkeypatch):
    output, state, calls = tiny_download
    stream.main()
    assert calls and not (output / ".aef-geotiff-work").exists()
    assert not list(output.rglob("*.zarr"))
    catalog = pd.read_parquet(state / "catalog.parquet")
    assert list(catalog.output_format) == ["geotiff"]
    assert "zarr_path" not in catalog.columns
    paths = json.loads(catalog.iloc[0].geotiff_paths)
    assert len(paths) == 2
    for year, path in zip([2024, 2025], paths):
        with rasterio.open(path) as raster:
            np.testing.assert_array_equal(raster.read(1), np.full((19, 17), year / 10000, np.float32))
            assert raster.crs.to_epsg() == 32717
            assert raster.count == 64
            assert raster.tags()["native_protocol"] == stream.NATIVE_PROTOCOL
            assert json.loads(raster.tags()["native_source_manifest"])["signature"] == catalog.iloc[0].native_source_signature
    payload = {"output_dir": str(output), "control_dir": str(state),
               "params": {"workflow": "grid", "grid_ids": ["TEST"], "years": [2024, 2025], "output_format": "geotiff"}}
    assert validate_delivery(payload)["valid"]
    before = [Path(path).stat().st_mtime_ns for path in paths]

    def no_network(*args, **kwargs):
        pytest.fail("Completed TIFF-only resume must not authenticate or download")

    monkeypatch.setattr(stream, "initialize", no_network)
    stream.main()
    assert before == [Path(path).stat().st_mtime_ns for path in paths]
    assert json.loads((state / "run_summary.json").read_text())["native_alignment_status"] == "completed_frozen_manifest"
    assert validate_delivery(payload)["valid"]
    with rasterio.open(paths[0], "r+") as raster:
        raster.transform = Affine(10, 0, 500010, 0, -10, 10000000)
    assert not validate_delivery(payload)["valid"]


def test_completed_tiff_requires_native_source_evidence(tiny_download, monkeypatch):
    output, _, calls = tiny_download
    stream.main()
    manifest_path = output / "geotiff_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["grids"][0]["native_source_manifest"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = len(calls)
    monkeypatch.setattr(stream, "initialize", lambda *args, **kwargs: pytest.fail("completed TIFF authentication"))
    with pytest.raises(ValueError, match="lacks verified native-source"):
        stream.main()
    assert len(calls) == before


def test_offline_plan_does_not_claim_native_verification(tiny_download, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", sys.argv + ["--plan-only"])
    monkeypatch.setattr(stream, "initialize", lambda *args, **kwargs: pytest.fail("offline plan authenticated"))
    stream.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["native_protocol"] == stream.NATIVE_PROTOCOL
    assert plan["native_alignment_status"] == "unverified_offline_plan"


def test_tiff_only_cleanup_failure_can_resume_after_partial_cleanup(tiny_download, monkeypatch):
    output, state, calls = tiny_download
    original = geotiff.shutil.rmtree

    def interrupted(path, *args, **kwargs):
        if Path(path).name == ".aef-geotiff-work":
            (Path(path) / "catalog.parquet").unlink()
            raise OSError("simulated cleanup interruption")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(geotiff.shutil, "rmtree", interrupted)
    with pytest.raises(OSError, match="cleanup interruption"):
        stream.main()
    assert (output / ".aef-geotiff-work.json").exists()
    assert (output / ".aef-geotiff-work").exists()
    manifest = json.loads((output / "geotiff_manifest.json").read_text(encoding="utf-8"))
    times = [Path(row["path"]).stat().st_mtime_ns for row in manifest["grids"][0]["files"]]
    request_count = len(calls)
    monkeypatch.setattr(geotiff.shutil, "rmtree", original)
    monkeypatch.setattr(stream, "initialize", lambda *args, **kwargs: pytest.fail("network during cleanup resume"))
    stream.main()
    assert len(calls) == request_count
    assert not (output / ".aef-geotiff-work").exists()
    assert times == [Path(row["path"]).stat().st_mtime_ns for row in manifest["grids"][0]["files"]]
    assert json.loads((state / "run_summary.json").read_text(encoding="utf-8"))["geotiff_status"] == "complete"


def test_tiff_only_will_not_claim_existing_data_or_wrong_request(tmp_path):
    output = tmp_path / "output"
    cache = output / ".aef-geotiff-work"
    cache.mkdir(parents=True)
    valuable = cache / "keep.txt"
    valuable.write_text("existing data", encoding="utf-8")
    with pytest.raises(FileExistsError, match="new task"):
        geotiff.prepare_tiff_work(output, output / "catalog.parquet", "test")
    assert valuable.read_text(encoding="utf-8") == "existing data"
    fresh = tmp_path / "fresh"
    geotiff.prepare_tiff_work(fresh, fresh / "catalog.parquet", "first")
    with pytest.raises(ValueError, match="differs"):
        geotiff.prepare_tiff_work(fresh, fresh / "catalog.parquet", "other")
    with pytest.raises(ValueError, match="outside the temporary cache"):
        geotiff.prepare_tiff_work(fresh, fresh / ".aef-geotiff-work" / "catalog.parquet", "first")


def test_tiff_only_formats_are_mutually_exclusive(tiny_download, monkeypatch):
    monkeypatch.setattr(sys, "argv", sys.argv + ["--export-geotiff"])
    with pytest.raises(SystemExit) as error:
        stream.parse_args()
    assert error.value.code == 2


def test_tiff_only_refuses_permanent_zarr(tiny_download):
    output, _, _ = tiny_download
    permanent = output / "TEST" / "aef_TEST_2024_2025.zarr"
    permanent.mkdir(parents=True)
    marker = permanent / "keep.txt"
    marker.write_text("old result", encoding="utf-8")
    with pytest.raises(FileExistsError, match="permanent Zarr"):
        stream.main()
    assert marker.read_text(encoding="utf-8") == "old result"


def test_tiff_only_invalid_export_retains_download_cache(tiny_download, monkeypatch):
    output, state, _ = tiny_download
    original = geotiff.export_catalog

    def corrupt_after_export(*args, **kwargs):
        manifest = original(*args, **kwargs)
        with rasterio.open(manifest["grids"][0]["files"][0]["path"], "r+") as dataset:
            dataset.set_band_description(1, "invalid-band")
        return manifest

    monkeypatch.setattr(geotiff, "export_catalog", corrupt_after_export)
    with pytest.raises(ValueError, match="Invalid AEF GeoTIFF"):
        stream.main()
    assert (output / ".aef-geotiff-work" / "TEST" / "aef_TEST_2024_2025.zarr").exists()
    assert not (state / "catalog.parquet").exists()
    summary = json.loads((state / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["geotiff_status"] == "failed"


def test_tiff_only_cleanup_rejects_links(tiny_download, monkeypatch, tmp_path):
    output, _, _ = tiny_download
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "keep.txt").write_text("keep", encoding="utf-8")
    probe = tmp_path / "link-probe"
    try:
        probe.symlink_to(protected, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation requires platform privileges")
    probe.unlink()
    original = geotiff.export_catalog

    def link_after_export(*args, **kwargs):
        manifest = original(*args, **kwargs)
        (output / ".aef-geotiff-work" / "external").symlink_to(protected, target_is_directory=True)
        return manifest

    monkeypatch.setattr(geotiff, "export_catalog", link_after_export)
    with pytest.raises(ValueError, match="linked TIFF cache"):
        stream.main()
    assert (protected / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (output / ".aef-geotiff-work").exists()
