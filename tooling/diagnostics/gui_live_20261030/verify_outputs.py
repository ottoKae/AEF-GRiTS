"""Read real small-pilot deliveries; structural checks reuse the production validator."""

from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window

from aef_grits.atomic import write_json
from aef_grits.delivery_validation import validate_delivery
from aef_grits.earth_engine import feature_columns
from aef_grits.point_store import load_aef_points
from aef_grits.store import AEFZarr


def validate_points(output_dir, control_dir, params, expected_points=None):
    """Validate the tiny pilot tables, optionally against original WGS84 centres."""
    result = validate_delivery({"output_dir": str(output_dir), "control_dir": str(control_dir), "params": params})
    if not result["valid"]:
        raise ValueError("; ".join(result["errors"]))
    dataset = load_aef_points(Path(control_dir) / "catalog.parquet", years=params["years"], require_complete=True)
    frame = dataset.frame.set_index("sample_id")
    result["annual_values"] = {}
    for year in params["years"]:
        values = frame[feature_columns([year])].to_numpy(dtype=np.float32)
        assert np.isfinite(values).all(), "Point vectors contain NaN or infinity"
        norms = np.linalg.norm(values.astype(np.float64), axis=1)
        assert (norms > 0).all(), "All-zero point vector"
        result["annual_values"][str(year)] = {"rows": len(frame), "features": values.shape[1],
            "norm_min": float(norms.min()), "norm_max": float(norms.max())}
    if expected_points is not None:
        expected = expected_points.copy()
        expected["sample_id"] = expected.sample_id.astype(str)
        expected = expected.set_index("sample_id")
        assert set(expected.index) == set(frame.index), "Downloaded sample IDs differ from input"
        original = expected.loc[frame.index, ["lon", "lat"]].to_numpy(dtype=float)
        actual = frame[["lon", "lat"]].to_numpy(dtype=float)
        np.testing.assert_allclose(actual, original, atol=1e-7, rtol=0)
        result["source_coordinate_max_error_deg"] = float(np.abs(actual - original).max())
    result["unique_sample_ids"] = int(frame.index.nunique())
    return result


def validate_grid(output_dir, control_dir, params):
    """Scan bounded windows, not just headers; compare every TIFF value with its Zarr."""
    output_dir = Path(output_dir)
    result = validate_delivery({"output_dir": str(output_dir), "control_dir": str(control_dir), "params": params})
    if not result["valid"]:
        raise ValueError("; ".join(result["errors"]))
    catalog = pd.read_parquet(Path(control_dir) / "catalog.parquet")
    manifest = output_dir / "geotiff_manifest.json"
    tiffs = {grid["grid_id"]: grid for grid in json.loads(manifest.read_text(encoding="utf-8"))["grids"]} if manifest.exists() else {}
    scans = []
    for row in catalog.itertuples(index=False):
        store = AEFZarr(row.zarr_path) if params.get("output_format") != "geotiff" else None
        for year in params["years"]:
            files = tiffs.get(str(row.grid_id), {}).get("files", [])
            path = next((item["path"] for item in files if item["year"] == year), None)
            with rasterio.open(path) if path else nullcontext() as raster:
                transform = store.transform if store else raster.transform
                crs = rasterio.crs.CRS.from_user_input(store.crs) if store else raster.crs
                height, width = (store.height, store.width) if store else (raster.height, raster.width)
                assert crs.is_projected
                np.testing.assert_allclose([transform.a, transform.b, transform.d, transform.e], [10, 0, 0, -10], atol=0, rtol=0)
                finite_pixels, nan_pixels, norm_min, norm_max = 0, 0, float("inf"), 0.0
                for y in range(0, height, 256):
                    for x in range(0, width, 256):
                        h, w = min(256, height-y), min(256, width-x)
                        values = np.asarray(store.array[store.years.index(year), :, y:y+h, x:x+w]) if store else raster.read(window=Window(x, y, w, h))
                        assert values.dtype == np.float32 and values.shape == (64, h, w)
                        assert not np.isinf(values).any(), "Raster contains infinity"
                        valid = np.isfinite(values).all(axis=0)
                        finite_pixels += int(valid.sum())
                        nan_pixels += int((~valid).sum())
                        if valid.any():
                            norms = np.linalg.norm(values[:, valid].astype(np.float64), axis=0)
                            assert (norms > 0).all(), "All-zero raster vector"
                            norm_min, norm_max = min(norm_min, float(norms.min())), max(norm_max, float(norms.max()))
                        if store and raster:
                            np.testing.assert_array_equal(values, raster.read(window=Window(x, y, w, h)))
                assert finite_pixels > 0, "The downloaded grid has no finite 64-D pixel"
                scans.append({"grid_id": str(row.grid_id), "year": year, "crs": str(crs),
                    "transform": list(transform)[:6], "shape": [64, height, width],
                    "finite_pixels": finite_pixels, "nonfinite_pixels": nan_pixels,
                    "finite_fraction": finite_pixels / (height * width),
                    "norm_min": norm_min, "norm_max": norm_max,
                    "tiff_zarr_equal": True if store and raster else None})
    result["pixel_scans"] = scans
    return result


def validate_acceptance(data_root):
    """Validate only completed live cases; pending cases remain explicitly unverified."""
    data_root = Path(data_root)
    records = json.loads((data_root / "acceptance.json").read_text(encoding="utf-8"))
    report = {"cases": {}, "pending": []}
    for case in ("csv", "shp", "map", "tessera", "reference"):
        task = records.get(case, {})
        if task.get("status") != "done":
            report["pending"].append(case)
            continue
        control = Path(task["download_state_dir"])
        if task["workflow"] == "points":
            source = control.parent / "input" / "map_points.csv" if case == "map" else data_root / "inputs" / "points.csv"
            report["cases"][case] = validate_points(task["output_dir"], control, task, pd.read_csv(source, dtype={"sample_id": str}))
        else:
            report["cases"][case] = validate_grid(task["output_dir"], control, task)
    if {"csv", "shp"} <= report["cases"].keys():
        csv = load_aef_points(Path(records["csv"]["download_state_dir"]) / "catalog.parquet").frame.set_index("sample_id").sort_index()
        shp = load_aef_points(Path(records["shp"]["download_state_dir"]) / "catalog.parquet").frame.set_index("sample_id").sort_index()
        columns = feature_columns(records["csv"]["years"])
        np.testing.assert_array_equal(csv.index, shp.index)
        np.testing.assert_array_equal(csv[columns].to_numpy(), shp[columns].to_numpy())
        report["csv_vs_projected_shp"] = {"equal": True, "rows": len(csv), "features_per_row": len(columns), "max_absolute_difference": 0.0}
    report["all_five_completed"] = not report["pending"]
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("validation_input", type=Path, nargs="?", help="The task's existing validation_input.json")
    parser.add_argument("--data-root", type=Path, help="Validate completed acceptance.json entries and write value_checks.json")
    args = parser.parse_args()
    if bool(args.data_root) == bool(args.validation_input):
        parser.error("Choose validation_input or --data-root, not both")
    if args.data_root:
        result = validate_acceptance(args.data_root)
        write_json(result, args.data_root / "value_checks.json")
    else:
        payload = json.loads(args.validation_input.read_text(encoding="utf-8"))
        validator = validate_points if payload["params"]["workflow"] == "points" else validate_grid
        result = validator(payload["output_dir"], payload["control_dir"], payload["params"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
