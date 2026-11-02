"""Bounded live native-grid audit; never modifies production downloads."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import numpy as np
import pandas as pd
from affine import Affine
from pyproj import CRS, Transformer

from aef_grits.atomic import write_json, write_parquet
from aef_grits.earth_engine import (
    AEF_BANDS, DATASET, RequestPolicy, annual_image, execute_with_retry,
    feature_columns, initialize, multiyear_image,
)
from aef_grits.grids import MGRSGridProvider, Tessera01GridProvider
from aef_grits.redaction import redact_text
from aef_grits.store import AEFZarr
from scripts.stream_aef_grid_ee import _pixel_grid
from scripts.stream_aef_points_ee import _bounds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--points", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[3]
    files = [Path(__file__), repo / "aef_grits/earth_engine.py",
             repo / "aef_grits/grids.py", repo / "scripts/stream_aef_points_ee.py",
             repo / "scripts/stream_aef_grid_ee.py"]
    receipt = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(), "python": sys.executable,
        "git_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "code_sha256": {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in files},
        "request_budget": 60, "workers": 1, "request_deadline_seconds": 60,
        "maximum_retries": 1, "raster_window": [16, 16, 64],
        "scope": "Five existing grid/year windows, four historical points, two boundary metadata probes",
        "archive": str(args.archive.resolve()), "historical_points": str(args.points.resolve()),
        "output": str(args.out.resolve()), "historical_data_policy": "read-only",
        "counter_excludes": "credential refresh and Earth Engine initialization",
    }
    write_json(receipt, args.out / "receipt.json")
    print("Initializing existing credentials; no login", flush=True)
    ee, auth = initialize(None, return_auth=True, request_timeout_seconds=60)
    receipt["authentication"] = auth.public_summary()
    write_json(receipt, args.out / "receipt.json")
    audit = Audit(ee, args.out)
    stores = [
        ("17MPU", args.archive / "outputs/compression_multitile_20260811/stores/17MPU/aef_17MPU_2017_2021_2025.zarr", [2017, 2025]),
        ("17NQA", args.archive / "outputs/compression_multitile_20260811/stores/17NQA/aef_17NQA_2017_2021_2025.zarr", [2017, 2025]),
        ("tessera_probe", args.archive / "outputs/tile_smoke_20260811/tessera_0p1/grid_-79.95_-1.05__probe_256.zarr", [2025]),
    ]
    summary = {"grid_cases": [], "historical_points": {}, "zone_boundary": []}
    for name, path, years in stores:
        store = AEFZarr(path)
        for year in years:
            label = f"{name}_{year}"
            print(f"Checking {label}", flush=True)
            summary["grid_cases"].append(audit.grid_case(label, store, year, name))
            write_json(summary, args.out / "summary.json")
    summary["historical_points"] = audit.historical_points(pd.read_parquet(args.points))
    write_json(summary, args.out / "summary.json")
    for label, lon in (("zone49", 113.9999), ("zone50", 114.0001)):
        point = ee.Geometry.Point([lon, 30.55])
        sources = audit.sources(f"boundary_{label}", 2025, point)
        target = Tessera01GridProvider().get(lon, 30.55)
        summary["zone_boundary"].append({
            "label": label, "target_grid": target.grid_id,
            "sources": [{"id": src["id"], "grid_relation": grid_relation(
                src["bands"][0]["crs"], Affine(*src["bands"][0]["crs_transform"]),
                target.crs, target.transform)} for src in sources],
        })
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    summary["request_attempts"] = len(audit.events)
    summary["downloaded_array_bytes"] = sum(p.stat().st_size for p in args.out.rglob("*.npy"))
    summary["status"] = "complete"
    write_json(summary, args.out / "summary.json")
    print(json.dumps({"status": "complete", "request_attempts": len(audit.events),
                      "grid_cases": len(summary["grid_cases"])}), flush=True)


def grid_relation(source_crs, source_transform, target_crs, target_transform):
    if CRS.from_user_input(source_crs) != CRS.from_user_input(target_crs):
        return {"aligned": False, "reason": "different_crs", "source_crs": source_crs,
                "target_crs": str(target_crs)}
    relation = ~source_transform * target_transform
    linear = np.asarray([relation.a, relation.b, relation.d, relation.e])
    offset = np.asarray([relation.c, relation.f])
    same_linear = bool(np.allclose(linear, [1, 0, 0, 1], rtol=0, atol=1e-10))
    residual = np.abs(offset - np.rint(offset))
    return {"aligned": same_linear and bool((residual < 1e-6).all()),
            "source_crs": source_crs, "target_crs": str(target_crs),
            "same_pixel_basis": same_linear, "pixel_offset": offset.tolist(),
            "fractional_pixel_residual": residual.tolist()}


def compare(left, right):
    left, right = np.asarray(left, np.float32), np.asarray(right, np.float32)
    if left.shape != right.shape:
        raise ValueError(f"Comparison shapes differ: {left.shape} != {right.shape}")
    valid_left, valid_right = np.isfinite(left), np.isfinite(right)
    joint = valid_left & valid_right
    differences = np.abs(left[joint].astype(float) - right[joint].astype(float))
    vectors_left, vectors_right = left.reshape(-1, 64), right.reshape(-1, 64)
    complete = np.isfinite(vectors_left).all(axis=1) & np.isfinite(vectors_right).all(axis=1)
    equal_vectors = np.equal(vectors_left, vectors_right).all(axis=1) & complete
    return {"shape": list(left.shape), "total_vectors": len(vectors_left),
            "complete_pairs": int(complete.sum()), "exact_vectors": int(equal_vectors.sum()),
            "left_finite_values": int(valid_left.sum()), "right_finite_values": int(valid_right.sum()),
            "mask_disagreements": int(np.not_equal(valid_left, valid_right).sum()),
            "exact_finite_values": int(np.equal(left[joint], right[joint]).sum()),
            "max_abs_difference": float(differences.max()) if differences.size else None,
            "mean_abs_difference": float(differences.mean()) if differences.size else None}


class Audit:
    def __init__(self, ee, root):
        self.ee, self.root = ee, root
        self.events = json.loads((root / "requests.json").read_text()) if (root / "requests.json").exists() else []

    def fetch(self, label, function, kind="json"):
        path = self.root / f"{label}.{kind}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if kind == "npy":
                return np.load(path)
            if kind == "parquet":
                return pd.read_parquet(path)
            return json.loads(path.read_text(encoding="utf-8"))

        def request():
            if len(self.events) >= 60:
                raise RuntimeError("Audit request budget exhausted")
            event = {"operation": label, "utc": datetime.now(timezone.utc).isoformat()}
            self.events.append(event)
            write_json(self.events, self.root / "requests.json")
            started = time.perf_counter()
            try:
                result = function()
                event["status"] = "ok"
                return result
            except Exception as exc:
                event.update(status="failed", error=redact_text(exc))
                raise
            finally:
                event["seconds"] = round(time.perf_counter() - started, 3)
                write_json(self.events, self.root / "requests.json")

        result = execute_with_retry(label, request, RequestPolicy(timeout_seconds=60, max_retries=1))
        if kind == "npy":
            np.save(path, result)
        elif kind == "parquet":
            write_parquet(result, path)
        else:
            write_json(result, path)
        return result

    def sources(self, label, year, geometry):
        collection = (self.ee.ImageCollection(DATASET)
                      .filterDate(f"{year}-01-01", f"{year + 1}-01-01").filterBounds(geometry))
        ids = self.fetch(f"metadata/{label}_asset_ids", lambda: collection.limit(5).aggregate_array("system:id").getInfo())
        if len(ids) >= 5:
            raise ValueError("Too many source assets for the bounded audit")
        sources = []
        for asset_id in ids:
            key = hashlib.sha256(asset_id.encode()).hexdigest()[:16]
            info = self.fetch(f"metadata/source_{key}", lambda: self.ee.Image(asset_id).select(list(AEF_BANDS)).getInfo())
            sources.append(info)
        return sources

    def pixels(self, label, image, crs, transform):
        grid = {"crs": crs, "transform": transform}
        block = {"width": 16, "height": 16, "row": 0, "column": 0}
        write_json({"crs": crs, "transform": list(transform)[:6], "shape": [16, 16, 64]},
                   self.root / f"{label}_grid.json")

        def request():
            result = self.ee.data.computePixels({"expression": image.unmask(-9999, sameFootprint=False),
                "fileFormat": "NUMPY_NDARRAY", "bandIds": list(AEF_BANDS), "grid": _pixel_grid(grid, block)})
            values = np.stack([result[band] for band in AEF_BANDS], axis=-1).astype(np.float32)
            assert values.shape == (16, 16, 64)
            values[values == -9999] = np.nan
            return values
        return self.fetch(label, request, "npy")

    def sample(self, label, image, points, columns, projection=None, scale=None, point_crs="EPSG:4326"):
        features = self.ee.FeatureCollection([self.ee.Feature(
            self.ee.Geometry.Point([float(row.x), float(row.y)], point_crs),
            {"sample_id": str(row.sample_id)}) for row in points.itertuples(index=False)])
        options = {"collection": features, "properties": ["sample_id"], "geometries": False,
                   "tileScale": 8}
        if projection is not None:
            options["projection"] = projection
        if scale is not None:
            options["scale"] = scale
        expression = image.sampleRegions(**options)
        frame = self.fetch(label, lambda: self.ee.data.computeFeatures({"expression": expression,
            "fileFormat": "PANDAS_DATAFRAME", "pageSize": 1000}), "parquet")
        if frame.empty:
            return np.full((len(points), 64), np.nan, np.float32)
        assert not frame.sample_id.duplicated().any()
        return frame.set_index("sample_id").reindex(points.sample_id).reindex(columns=columns).to_numpy(np.float32)

    def grid_case(self, label, store, year, tile_name):
        ee = self.ee
        row, col = store.height // 2 - 8, store.width // 2 - 8
        target = store.transform * Affine.translation(col, row)
        x, y = target * (8.5, 8.5)
        lon, lat = Transformer.from_crs(store.crs, 4326, always_xy=True).transform(x, y)
        sources = self.sources(label, year, ee.Geometry.Point([lon, lat]))
        record = {"case": label, "year": year, "local_store": str(store.path),
                  "local_scheme": store.scheme, "local_crs": store.crs,
                  "local_transform": list(store.transform)[:6], "source_count": len(sources), "sources": []}
        for source in sources:
            band = source["bands"][0]
            source_crs, source_t = band["crs"], Affine(*band["crs_transform"])
            entry = {"id": source["id"], "crs": source_crs, "transform": list(source_t)[:6],
                "all_bands_same_grid": all(b["crs"] == source_crs and b["crs_transform"] == band["crs_transform"] for b in source["bands"]),
                "local_grid_relation": grid_relation(source_crs, source_t, store.crs, store.transform)}
            tessera = Tessera01GridProvider().get(lon, lat)
            entry["tessera_relation"] = grid_relation(source_crs, source_t, tessera.crs, tessera.transform)
            if tile_name in {"17MPU", "17NQA"}:
                mgrs = MGRSGridProvider().get(tile_name)
                entry["mgrs_relation"] = grid_relation(source_crs, source_t, mgrs.crs, mgrs.transform)
            record["sources"].append(entry)
        if len(sources) != 1:
            record["value_check"] = "not_attempted_ambiguous_or_missing_source"
            return record
        source = sources[0]
        image = ee.Image(source["id"]).select(list(AEF_BANDS)).toFloat()
        source_crs = source["bands"][0]["crs"]
        source_t = Affine(*source["bands"][0]["crs_transform"])
        sx, sy = Transformer.from_crs(store.crs, source_crs, always_xy=True).transform(x, y)
        sc, sr = ~source_t * (sx, sy)
        native_t = source_t * Affine.translation(int(np.floor(sc)) - 8, int(np.floor(sr)) - 8)
        native = self.pixels(f"{year}/{label}_native", image, source_crs, native_t)
        positions = [(r, c) for r in (2, 8, 13) for c in (2, 8, 13)]
        points = pd.DataFrame([{"sample_id": f"p{r}_{c}", "x": (native_t * (c + .5, r + .5))[0],
                                "y": (native_t * (c + .5, r + .5))[1]} for r, c in positions])
        values = self.sample(f"{year}/{label}_native_centres", image, points, AEF_BANDS,
                             projection=image.select("A00").projection(), point_crs=source_crs)
        record["native_centres_vs_native_raster"] = compare(values, np.asarray([native[r, c] for r, c in positions]))
        local = np.moveaxis(store.read_window(row, row + 16, col, col + 16, years=[year])[0], 0, -1)
        if native_t == target and source_crs == store.crs:
            on_target = native
        else:
            on_target = self.pixels(f"{year}/{label}_source_on_target", image, store.crs, target)
        record["historical_raster_vs_same_source_now"] = compare(local, on_target)
        # Reproduce the historical grid mosaic footprint, not only its centre point.
        bounds = ee.Geometry.Rectangle(
            [store.transform.c, store.transform.f + store.height * store.transform.e,
             store.transform.c + store.width * store.transform.a, store.transform.f], store.crs, False)
        mosaic = self.pixels(f"{year}/{label}_current_mosaic", annual_image(year, bounds), store.crs, target)
        record["historical_raster_vs_current_mosaic"] = compare(local, mosaic)
        record["source_vs_mosaic_on_target"] = compare(on_target, mosaic)
        return record

    def historical_points(self, historical):
        ee, year = self.ee, 2025
        frame = historical[["sample_id", "lon", "lat"]].copy()
        frame.sample_id = frame.sample_id.astype(str)
        points = frame.rename(columns={"lon": "x", "lat": "y"})
        bounds = _bounds(ee, frame)
        columns = feature_columns([year])
        sources = self.sources("historical_four_points", year, bounds)
        current = self.sample("2025/four_points_current", multiyear_image([year], bounds), points, columns, scale=10)
        old = historical[columns].to_numpy(np.float32)
        record = {"rows": len(frame), "source_count": len(sources),
                  "historical_vs_current_protocol": compare(old, current), "sources": []}
        write_parquet(frame, self.root / "historical_point_coordinates.parquet")
        for index, source in enumerate(sources):
            image = ee.Image(source["id"]).select(list(AEF_BANDS)).toFloat()
            band = source["bands"][0]
            projection = image.select("A00").projection()
            native = self.sample(f"2025/four_points_native_{index}", image, points, AEF_BANDS, projection=projection)
            source_t = Affine(*band["crs_transform"])
            x, y = Transformer.from_crs(4326, band["crs"], always_xy=True).transform(frame.lon.to_numpy(), frame.lat.to_numpy())
            col, row = ~source_t * (np.asarray(x), np.asarray(y))
            entry = {"id": source["id"], "crs": band["crs"], "transform": list(source_t)[:6],
                     "historical_vs_native": compare(old, native), "current_vs_native": compare(current, native),
                     "source_pixels": [{"sample_id": sid, "column": int(np.floor(c)), "row": int(np.floor(r))}
                                       for sid, c, r in zip(frame.sample_id, col, row)]}
            native_checks = []
            for i in range(len(points)):
                transform = source_t * Affine.translation(int(np.floor(col[i])) - 8, int(np.floor(row[i])) - 8)
                block = self.pixels(f"2025/point_{i}_source_{index}_native", image, band["crs"], transform)
                native_checks.append(block[8, 8])
            entry["native_points_vs_native_raster"] = compare(native, native_checks)
            record["sources"].append(entry)
        return record


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "stopped", "error": redact_text(error)}), flush=True)
        raise SystemExit(1)
