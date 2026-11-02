"""Verify cell footprints independently of array orientation after the live audit."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from affine import Affine
from pyproj import CRS, Transformer

from aef_grits.atomic import write_json
from aef_grits.earth_engine import AEF_BANDS, annual_image, initialize, multiyear_image
from aef_grits.grids import MGRSGridProvider, Tessera01GridProvider
from aef_grits.redaction import redact_text
from aef_grits.store import AEFZarr
from scripts.stream_aef_points_ee import _bounds
from tooling.diagnostics.native_alignment_20261101.audit import Audit, compare


def footprint_relation(source_crs, source_transform, target_crs, target_transform):
    if CRS.from_user_input(source_crs) != CRS.from_user_input(target_crs):
        return {"same_pixel_footprints": False, "reason": "different_crs"}
    relation = ~source_transform * target_transform
    matrix = np.asarray([[relation.a, relation.b], [relation.d, relation.e]])
    rounded = np.rint(matrix)
    permutation = (np.allclose(matrix, rounded, rtol=0, atol=1e-10)
                   and np.equal(np.abs(rounded).sum(axis=0), 1).all()
                   and np.equal(np.abs(rounded).sum(axis=1), 1).all())
    translation = np.asarray([relation.c, relation.f])
    residual = np.abs(translation - np.rint(translation))
    return {"same_pixel_footprints": bool(permutation and (residual < 1e-6).all()),
            "same_array_orientation": bool(np.allclose(matrix, np.eye(2), rtol=0, atol=1e-10)),
            "target_to_source_index_relation": list(relation)[:6],
            "fractional_pixel_residual": residual.tolist()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    original = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    assert original["status"] == "complete"
    source_info = {item["id"]: item for path in (root / "metadata").glob("source_*.json")
                   for item in [json.loads(path.read_text(encoding="utf-8"))]}
    ee, auth = initialize(None, return_auth=True, request_timeout_seconds=60)
    audit = Audit(ee, root)
    result = {"started_utc": datetime.now(timezone.utc).isoformat(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "authentication": auth.public_summary(), "grid_cases": [], "point_locations": [],
              "zone_boundary": [],
              "interpretation": "Stage-A aligned checks identical array axes; this check allows lossless row/column reindexing.",
              "revision_note": "v2 corrects the index-relation field name; unchanged cached request artifacts are reused."}
    for case in original["grid_cases"]:
        print(f"Footprint verification: {case['case']}", flush=True)
        assert case["source_count"] == 1 and case["sources"][0]["all_bands_same_grid"]
        source = case["sources"][0]
        store = AEFZarr(case["local_store"])
        source_t = Affine(*source["transform"])
        row, col = store.height // 2 - 8, store.width // 2 - 8
        target = store.transform * Affine.translation(col, row)
        relation = footprint_relation(source["crs"], source_t, store.crs, target)
        entry = {"case": case["case"], "local_relation": relation}
        x, y = target * (8.5, 8.5)
        lon, lat = Transformer.from_crs(store.crs, 4326, always_xy=True).transform(x, y)
        tessera = Tessera01GridProvider().get(lon, lat)
        entry["tessera_relation"] = footprint_relation(source["crs"], source_t, tessera.crs, tessera.transform)
        if case["case"].split("_")[0] in {"17MPU", "17NQA"}:
            mgrs = MGRSGridProvider().get(case["case"].split("_")[0])
            entry["mgrs_relation"] = footprint_relation(source["crs"], source_t, mgrs.crs, mgrs.transform)
        assert relation["same_pixel_footprints"], "Do not reindex incompatible grids"
        transform = ~source_t * target
        corners = np.asarray([transform * xy for xy in ((0, 0), (16, 0), (0, 16), (16, 16))])
        start = np.rint(corners.min(axis=0)).astype(int)
        assert np.equal(np.rint(corners.max(axis=0)).astype(int) - start, 16).all()
        native_t = source_t * Affine.translation(*start)
        image = ee.Image(source["id"]).select(list(AEF_BANDS)).toFloat()
        native = audit.pixels(f"{case['year']}/{case['case']}_exact_extent_native", image, source["crs"], native_t)
        rr, cc = np.indices((16, 16))
        source_cc, source_rr = transform * (cc + .5, rr + .5)
        reindexed = native[np.floor(source_rr).astype(int) - start[1], np.floor(source_cc).astype(int) - start[0]]
        local = np.moveaxis(store.read_window(row, row + 16, col, col + 16, years=[case["year"]])[0], 0, -1)
        entry["original_native_pixels_vs_historical_raster"] = compare(reindexed, local)
        result["grid_cases"].append(entry)

    frame = pd.read_parquet(root / "historical_point_coordinates.parquet")
    image = multiyear_image([2025], _bounds(ee, frame))
    features = ee.FeatureCollection([ee.Feature(ee.Geometry.Point([float(p.lon), float(p.lat)], "EPSG:4326"),
        {"sample_id": str(p.sample_id)}) for p in frame.itertuples(index=False)])
    sample = image.sampleRegions(collection=features, properties=["sample_id"], scale=10, tileScale=8, geometries=True)
    sampled = audit.fetch("2025/four_points_current_geometry", sample.getInfo)
    actual = {f["properties"]["sample_id"]: f for f in sampled["features"]}
    source = original["historical_points"]["sources"][0]
    source_t = Affine(*source["transform"])
    to_source = Transformer.from_crs(4326, source["crs"], always_xy=True)
    current = pd.read_parquet(root / "2025/four_points_current.parquet").set_index("sample_id")
    columns = [f"aef2025_center_{band}" for band in AEF_BANDS]
    for index, point in enumerate(frame.itertuples(index=False)):
        original_xy = to_source.transform(point.lon, point.lat)
        original_ij = np.floor(~source_t * original_xy).astype(int)
        center_lonlat = actual[str(point.sample_id)]["geometry"]["coordinates"]
        center_xy = to_source.transform(*center_lonlat)
        sampled_ij = np.floor(~source_t * center_xy).astype(int)
        old_vector = current.loc[str(point.sample_id), columns].to_numpy(np.float32)
        block = np.load(root / f"2025/point_{index}_source_0_native.npy")
        matches = np.argwhere(np.equal(block, old_vector).all(axis=2))
        result["point_locations"].append({"sample_id": str(point.sample_id),
            "native_pixel": original_ij.tolist(), "current_sample_pixel": sampled_ij.tolist(),
            "pixel_offset_column_row": (sampled_ij - original_ij).tolist(),
            "sampling_center_displacement_m": float(np.linalg.norm(np.asarray(center_xy) - original_xy)),
            "matching_native_window_rows_columns": matches.tolist(),
            "expected_window_row_column": (sampled_ij - original_ij + 8)[::-1].tolist()})

    for label, lon in (("zone49", 113.9999), ("zone50", 114.0001)):
        target = Tessera01GridProvider().get(lon, 30.55)
        x, y = Transformer.from_crs(4326, target.crs, always_xy=True).transform(lon, 30.55)
        col, row = np.floor(~target.transform * (x, y)).astype(int)
        col, row = max(0, min(col - 8, target.width - 16)), max(0, min(row - 8, target.height - 16))
        window_t = target.transform * Affine.translation(col, row)
        bounds = ee.Geometry.Rectangle(list(target.bounds), target.crs, False)
        mosaic = audit.pixels(f"2025/boundary_{label}_mosaic", annual_image(2025, bounds), target.crs, window_t)
        entry = {"label": label, "target_crs": target.crs, "sources": []}
        ids = json.loads((root / f"metadata/boundary_{label}_asset_ids.json").read_text())
        for asset_id in ids:
            info = source_info[asset_id]
            band = info["bands"][0]
            key = hashlib.sha256(asset_id.encode()).hexdigest()[:16]
            array = audit.pixels(f"2025/boundary_{label}_{key}", ee.Image(asset_id).select(list(AEF_BANDS)).toFloat(), target.crs, window_t)
            entry["sources"].append({"id": asset_id, "source_crs": band["crs"],
                "relation": footprint_relation(band["crs"], Affine(*band["crs_transform"]), target.crs, window_t),
                "mosaic_vs_source_on_target": compare(mosaic, array)})
        result["zone_boundary"].append(entry)
    result["finished_utc"] = datetime.now(timezone.utc).isoformat()
    result["total_request_attempts_including_stage_a"] = len(audit.events)
    result["status"] = "complete"
    write_json(result, root / "native_footprint_verification_v2.json")
    print(json.dumps({"status": "complete", "total_request_attempts": len(audit.events)}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "stopped", "error": redact_text(error)}), flush=True)
        raise SystemExit(1)
