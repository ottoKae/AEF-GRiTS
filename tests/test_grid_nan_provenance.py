"""Distinguish successful source NoData from unfilled interrupted downloads."""

import json
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import zarr
from affine import Affine

from aef_grits.delivery_validation import validate_delivery
from aef_grits.earth_engine import EarthEngineRequestError, execute_with_retry
from aef_grits.grids import GridSpec
from aef_grits.store import AEFZarr
import scripts.stream_aef_grid_ee as grid_stream


@pytest.fixture
def stream_case(tmp_path, monkeypatch, native_grid_sources):
    grid = GridSpec(
        scheme="reference",
        grid_id="NAN_TEST",
        crs="EPSG:32650",
        transform=Affine(10, 0, 500_000, 0, -10, 3_400_000),
        width=4,
        height=4,
        bounds=(500_000, 3_399_960, 500_040, 3_400_000),
    )
    args = SimpleNamespace(
        block_size=4,
        inner_chunk=2,
        shard_size=4,
        max_retries=1,
        adaptive_request_splitting=False,
        workers=1,
        checkpoint_every=1,
        commit_timeout=30,
        adopt_existing_complete=False,
        import_legacy_progress=None,
        keep_staging=False,
    )

    class FakeImage:
        def unmask(self, value, *, sameFootprint):
            assert value == grid_stream.NODATA
            assert sameFootprint is False
            return self

    class FakeData:
        calls = 0
        error = None

        def computePixels(self, request):
            self.calls += 1
            if self.error is not None:
                raise self.error
            dimensions = request["grid"]["dimensions"]
            return {
                band: np.full(
                    (dimensions["height"], dimensions["width"]),
                    grid_stream.NODATA,
                    dtype=np.float32,
                )
                for band in grid_stream.AEF_BANDS
            }

    data = FakeData()
    ee = SimpleNamespace(
        Geometry=SimpleNamespace(Rectangle=lambda *args, **kwargs: object()),
        data=data,
    )
    sources = native_grid_sources(ee, grid.as_dict(), [2025], lambda year: FakeImage())
    monkeypatch.setattr(
        grid_stream,
        "execute_with_retry",
        partial(execute_with_retry, sleep=lambda seconds: None),
    )
    output = tmp_path / "final" / "tile.zarr"
    state = tmp_path / "state"
    catalog = state / "catalog.parquet"

    def run():
        return grid_stream._stream_one(
            args, [2025], grid, output, catalog, state, None, ee
        )

    return SimpleNamespace(
        run=run,
        data=data,
        output=output,
        state=state,
        catalog=catalog,
        progress=state / "grids" / grid.grid_id / "tile.zarr.progress.json",
        grid=grid,
        sources=sources,
    )


def test_source_nodata_is_nan_but_counts_as_complete(stream_case):
    case = stream_case
    report = case.run()

    assert case.data.calls == 1
    assert np.isnan(zarr.open_group(str(case.output), mode="r")["embeddings"][:]).all()
    progress = json.loads(case.progress.read_text(encoding="utf-8"))
    assert progress["completed"] == ["2025:0:0"]
    assert progress["delivery_status"] == "committed"
    assert report["status"] == "complete"
    assert report["valid_pixels_by_year"] == {"2025": 0}
    assert report["valid_fraction_by_year"] == {"2025": 0.0}
    assert report["total_pixels_per_year"] == 16
    assert pd.read_parquet(case.catalog).status.tolist() == ["complete"]


def test_source_nodata_passes_structural_delivery_validation(stream_case):
    pytest.importorskip("dask.array", reason="grid delivery validation uses Dask chunks")
    case = stream_case
    case.run()
    # The outer main() writes this summary after _stream_one() returns.
    (case.state / "run_summary.json").write_text(
        json.dumps({"completed": 1, "failed": []}), encoding="utf-8"
    )
    validation = validate_delivery(
        {
            "output_dir": str(case.output.parent),
            "control_dir": str(case.state),
            "params": {
                "workflow": "grid",
                "years": [2025],
                "grid_ids": [case.grid.grid_id],
            },
        }
    )
    assert validation["valid"], validation["errors"]
    assert validation["warnings"] == []


def test_connection_failure_leaves_unwritten_nan_without_completion(stream_case):
    case = stream_case
    case.data.error = ConnectionError("simulated connection reset")

    with pytest.raises(EarthEngineRequestError) as error:
        case.run()

    assert case.data.calls == 2
    assert error.value.resumable
    assert error.value.attempts == 2
    assert not case.catalog.exists()
    assert not list(case.progress.parent.glob("*.report.json"))
    assert not (case.output / "AEF_COMPLETE.json").exists()
    if case.progress.exists():
        progress = json.loads(case.progress.read_text(encoding="utf-8"))
        assert progress["completed"] == []
        assert progress["delivery_status"] != "committed"
    # The ordinary reader can still open this unfinished store and see NaN.
    store = AEFZarr(case.output)
    assert np.isnan(store.read_window(0, 4, 0, 4)).all()


def test_resume_does_not_refetch_completed_source_nodata(stream_case):
    case = stream_case
    first = case.run()
    case.data.error = AssertionError("a committed block must not be requested again")

    resumed = case.run()

    assert case.data.calls == 1
    assert first["status"] == resumed["status"] == "complete"
    assert np.isnan(zarr.open_group(str(case.output), mode="r")["embeddings"][:]).all()
    progress = json.loads(Path(resumed["progress"]).read_text(encoding="utf-8"))
    assert progress["completed"] == ["2025:0:0"]
    assert progress["delivery_status"] == "committed"


def test_no_compatible_native_source_rejects_before_creating_store(stream_case):
    case = stream_case
    case.sources[2025] = [replace(case.sources[2025][0], crs="EPSG:32649")]
    with pytest.raises(ValueError, match="No native AEF source matches"):
        case.run()
    assert not case.output.exists()
    assert case.data.calls == 0


def test_incompatible_sources_are_excluded_and_recorded(stream_case):
    case = stream_case
    compatible = case.sources[2025][0]
    case.sources[2025].extend([
        replace(compatible, id="foreign-zone", crs="EPSG:32649"),
        replace(compatible, id="half-pixel", transform=compatible.transform * Affine.translation(0.5, 0)),
    ])
    report = case.run()
    manifest = report["native_source_manifest"]
    annual = manifest["years"]["2025"]
    assert [row["id"] for row in annual["sources"]] == [compatible.id]
    assert {row["id"] for row in annual["excluded_sources"]} == {"foreign-zone", "half-pixel"}
    assert all(row["reason"] == "incompatible_crs_or_pixel_grid" for row in annual["excluded_sources"])
    group = zarr.open_group(str(case.output), mode="r")
    assert group.attrs["aef:native_source_manifest"] == manifest
    assert json.loads(case.progress.read_text())["native_source_manifest"] == manifest
    assert json.loads(pd.read_parquet(case.catalog).iloc[0].native_source_manifest) == manifest


def test_changed_source_manifest_refuses_completed_resume(stream_case):
    case = stream_case
    case.run()
    case.sources[2025][0] = replace(case.sources[2025][0], version=2)
    with pytest.raises(ValueError, match="Progress signature differs"):
        case.run()
    assert case.data.calls == 1


def test_completed_ledger_cannot_override_missing_native_store_evidence(stream_case):
    case = stream_case
    case.run()
    group = zarr.open_group(str(case.output), mode="r+", use_consolidated=False)
    del group.attrs["aef:native_protocol"]
    with pytest.raises(RuntimeError, match="adoption exited with code"):
        case.run()
    assert case.data.calls == 1
