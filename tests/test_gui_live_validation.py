import json

from affine import Affine
import numpy as np
import pandas as pd
import pytest
import zarr

from aef_grits.earth_engine import feature_columns
from aef_grits.geotiff import export_catalog
from scripts.stream_aef_grid_ee import _create_store
from tooling.diagnostics.gui_live_20261030.verify_outputs import validate_acceptance, validate_grid, validate_points


def test_pilot_points_are_read_and_coordinates_checked(tmp_path):
    output, control = tmp_path / "output", tmp_path / "state"
    output.mkdir()
    control.mkdir()
    expected = pd.DataFrame({"sample_id": ["p1"], "lon": [117.05], "lat": [31.05]})
    frame = pd.concat([expected, pd.DataFrame([[.125] * 64], columns=feature_columns([2025]))], axis=1)
    shard = output / "points.parquet"
    frame.to_parquet(shard, index=False)
    pd.DataFrame([{"path": str(shard), "rows": 1}]).to_parquet(control / "catalog.parquet", index=False)
    (control / "report.json").write_text(json.dumps({"sample_count": 1, "complete_rows": 1, "incomplete_rows": 0}), encoding="utf-8")
    params = {"workflow": "points", "years": [2025], "sample_count": 1}
    result = validate_points(output, control, params, expected)
    assert result["unique_sample_ids"] == 1
    assert result["annual_values"]["2025"]["norm_min"] == 1
    assert result["source_coordinate_max_error_deg"] == 0
    (tmp_path / "inputs").mkdir()
    expected.to_csv(tmp_path / "inputs" / "points.csv", index=False)
    (tmp_path / "acceptance.json").write_text(json.dumps({"csv": dict(params, status="done", output_dir=str(output), download_state_dir=str(control))}), encoding="utf-8")
    partial = validate_acceptance(tmp_path)
    assert list(partial["cases"]) == ["csv"]
    assert partial["pending"] == ["shp", "map", "tessera", "reference"]
    assert not partial["all_five_completed"]
    expected.loc[0, "lon"] += .001
    with pytest.raises(AssertionError):
        validate_points(output, control, params, expected)


def test_pilot_grid_scans_values_and_compares_tiff(tmp_path):
    output, control = tmp_path / "output", tmp_path / "state"
    control.mkdir()
    source = output / "reference" / "pilot.zarr"
    grid = {"width": 9, "height": 8, "crs": "EPSG:32650", "grid_id": "pilot",
            "transform": Affine(10, 0, 500000, 0, -10, 3440000)}
    group = _create_store(source, grid, [2025], "pilot-v1", 64, 512)
    values = np.full((64, 8, 9), .125, dtype=np.float32)
    values[:, 0, 0] = np.nan
    group["embeddings"][0] = values
    zarr.consolidate_metadata(source)
    catalog = control / "catalog.parquet"
    pd.DataFrame([{"grid_id": "pilot", "status": "complete", "zarr_path": str(source.resolve()),
        "crs": grid["crs"], "resolution_m": 10, "compression_codec": "zstd",
        "compression_level": 7, "compression_shuffle": "noshuffle"}]).to_parquet(catalog, index=False)
    (control / "run_summary.json").write_text(json.dumps({"completed": 1, "failed": []}), encoding="utf-8")
    export_catalog(catalog, output)
    params = {"workflow": "grid", "grid_ids": ["pilot"], "years": [2025], "output_format": "zarr+geotiff"}
    result = validate_grid(output, control, params)
    assert result["pixel_scans"][0]["finite_pixels"] == 71
    assert result["pixel_scans"][0]["tiff_zarr_equal"] is True
    group["embeddings"][0, 0, 1, 1] = .25
    with pytest.raises(AssertionError):
        validate_grid(output, control, params)
