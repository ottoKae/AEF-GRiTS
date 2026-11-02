"""Windowed, lossless GeoTIFF delivery of completed AEF Zarr grids."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

import numpy as np
import rasterio
from rasterio.windows import Window

from .store import AEFZarr


def _export_signature(store: AEFZarr, year: int) -> str:
    return hashlib.sha256(json.dumps({
        "source": str(store.path.resolve()),
        "signature": store.group.attrs.get("aef:signature"),
        "year": year, "shape": store.array.shape,
        "crs": store.crs, "transform": list(store.transform)[:6],
    }, sort_keys=True).encode()).hexdigest()


def _valid_header(dataset, store: AEFZarr, year: int) -> bool:
    tags = dataset.tags()
    return bool(
        dataset.count == 64 and dataset.dtypes == ("float32",) * 64
        and dataset.width == store.width and dataset.height == store.height
        and dataset.crs == rasterio.crs.CRS.from_user_input(store.crs)
        and dataset.transform == store.transform
        and dataset.descriptions == tuple(f"A{band:02d}" for band in range(64))
        and dataset.nodata is not None and np.isnan(dataset.nodata)
        and tags.get("year") == str(year) and tags.get("grid_id") == store.grid_id
        and tags.get("source_zarr") == str(store.path.resolve())
        and tags.get("aef_export_signature") == _export_signature(store, year)
        and tags.get("native_source_signature", "") == store.group.attrs.get("aef:native_source_signature", "")
        and tags.get("native_protocol", "") == store.group.attrs.get("aef:native_protocol", "")
        and json.loads(tags.get("native_source_manifest", "{}")) == store.group.attrs.get("aef:native_source_manifest", {})
    )


def export_geotiffs(store_path: str | Path, output_dir: str | Path) -> list[dict]:
    """Write one 64-band float32 TIFF per year, retaining the resumable Zarr."""
    store = AEFZarr(store_path)
    if store.array.shape != (len(store.years), 64, store.height, store.width):
        raise ValueError("Expected a [year,64,y,x] AEF store")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for time_index, year in enumerate(store.years):
        target = output_dir / f"{year}.tif"
        signature = _export_signature(store, year)
        if target.exists():
            with rasterio.open(target) as existing:
                if not _valid_header(existing, store, year):
                    raise FileExistsError(f"Refusing to replace unrelated GeoTIFF: {target}")
        else:
            handle, temp_name = tempfile.mkstemp(prefix=f".{year}-", suffix=".tif", dir=output_dir)
            os.close(handle)
            temporary = Path(temp_name)
            try:
                with rasterio.open(
                    temporary, "w", driver="GTiff", width=store.width, height=store.height,
                    count=64, dtype="float32", crs=store.crs, transform=store.transform,
                    nodata=np.nan, tiled=True, blockxsize=256, blockysize=256,
                    compress="DEFLATE", predictor=3,
                    BIGTIFF="YES" if store.width * store.height * 64 * 4 >= 2**32 else "IF_SAFER",
                ) as destination:
                    destination.descriptions = tuple(f"A{band:02d}" for band in range(64))
                    destination.update_tags(
                        aef_export_signature=signature, year=str(year), grid_id=store.grid_id,
                        source_zarr=str(store.path.resolve()),
                        source_dataset=str(store.group.attrs.get("aef:asset", "")),
                        native_protocol=str(store.group.attrs.get("aef:native_protocol", "")),
                        native_source_signature=str(store.group.attrs.get("aef:native_source_signature", "")),
                        native_source_manifest=json.dumps(store.group.attrs.get("aef:native_source_manifest", {}), sort_keys=True),
                    )
                    for row in range(0, store.height, 256):
                        for col in range(0, store.width, 256):
                            height, width = min(256, store.height-row), min(256, store.width-col)
                            values = np.asarray(store.array[time_index, :, row:row+height, col:col+width], dtype=np.float32)
                            destination.write(values, window=Window(col, row, width, height))
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        records.append({"year": year, "path": str(target.resolve()), "bytes": target.stat().st_size,
                        "export_signature": signature})
    return records


def export_catalog(catalog_path: str | Path, output_dir: str | Path, *, grid_ids: list[str] | None = None,
                   retain_zarr: bool = True, request_signature: str | None = None) -> dict:
    """Export a completed catalog, publishing a separate delivery manifest."""
    import pandas as pd
    from .atomic import write_json

    catalog = pd.read_parquet(catalog_path)
    if grid_ids is not None:
        catalog = catalog[catalog.grid_id.astype(str).isin(grid_ids)]
        if set(catalog.grid_id.astype(str)) != set(grid_ids):
            raise ValueError("Requested GeoTIFF grids are missing from the catalog")
    if catalog.empty or not catalog["status"].eq("complete").all():
        raise ValueError("GeoTIFF export requires a nonempty, completed grid catalog")
    output_dir = Path(output_dir).resolve()
    grids = []
    for row in catalog.itertuples(index=False):
        store_path = Path(row.zarr_path).resolve()
        if output_dir not in store_path.parents:
            raise ValueError("Zarr source is outside the task output directory")
        store = AEFZarr(store_path)
        if store.grid_id != str(row.grid_id):
            raise ValueError("Catalog grid ID differs from its Zarr source")
        # Each store owns its export directory, including multi-grid batches.
        destination = store_path.with_suffix(".geotiff")
        if not retain_zarr:
            destination = output_dir / store_path.relative_to(output_dir / ".aef-geotiff-work").with_suffix(".geotiff")
        files = export_geotiffs(store_path, destination)
        grids.append({"grid_id": str(row.grid_id), "zarr_path": str(store_path), "files": files,
                      "crs": store.crs, "transform": list(store.transform)[:6],
                      "width": store.width, "height": store.height, "years": store.years,
                      "source_dataset": str(store.group.attrs.get("aef:asset", "")),
                      "signature": str(store.group.attrs.get("aef:signature", "")),
                      "native_protocol": str(store.group.attrs.get("aef:native_protocol", "")),
                      "native_source_signature": str(store.group.attrs.get("aef:native_source_signature", "")),
                      "native_source_manifest": store.group.attrs.get("aef:native_source_manifest", {}),
                      "scheme": str(getattr(row, "scheme", "reference")), "resolution_m": 10})
        print("AEF_EVENT " + json.dumps({"event": "geotiff_complete", "grid_id": str(row.grid_id)}), flush=True)
    report = {"format": "zarr+geotiff" if retain_zarr else "geotiff",
              "zarr_retained": retain_zarr, "grids": grids}
    if not retain_zarr:
        report["request_signature"] = request_signature
    write_json(report, output_dir / "geotiff_manifest.json")
    return report


def validate_geotiffs(output_dir: Path, grid_ids: list[str], years: list[int]) -> list[str]:
    """Check manifest and raster structure, without scanning all pixel values."""
    errors = []
    try:
        report = json.loads((output_dir / "geotiff_manifest.json").read_text(encoding="utf-8"))
        grids = report["grids"]
        if sorted(row["grid_id"] for row in grids) != sorted(grid_ids):
            errors.append("GeoTIFF grid IDs differ from the plan")
        for grid in grids:
            source = Path(grid["zarr_path"]).resolve()
            if output_dir.resolve() not in source.parents:
                raise ValueError("GeoTIFF source path escapes the task output directory")
            standalone = report.get("format") == "geotiff" and report.get("zarr_retained") is False
            store = None if standalone else AEFZarr(source)
            if standalone:
                if sorted(grid["years"]) != sorted(years):
                    errors.append(f"GeoTIFF source differs from the plan: {grid['grid_id']}")
            elif store.grid_id != grid["grid_id"] or sorted(store.years) != sorted(years):
                errors.append(f"GeoTIFF source differs from the plan: {grid['grid_id']}")
            if sorted(row["year"] for row in grid["files"]) != sorted(years):
                errors.append(f"GeoTIFF years differ from the plan: {grid['grid_id']}")
            for record in grid["files"]:
                path = Path(record["path"]).resolve()
                if output_dir.resolve() not in path.parents:
                    raise ValueError("GeoTIFF path escapes the task output directory")
                if path.stat().st_size != record["bytes"]:
                    errors.append(f"GeoTIFF file size differs from the manifest: {path.name}")
                with rasterio.open(path) as dataset:
                    valid = _valid_header(dataset, store, int(record["year"])) if store else (
                        dataset.count == 64 and dataset.dtypes == ("float32",) * 64
                        and dataset.width == grid["width"] and dataset.height == grid["height"]
                        and dataset.crs == rasterio.crs.CRS.from_user_input(grid["crs"])
                        and list(dataset.transform)[:6] == grid["transform"]
                        and dataset.descriptions == tuple(f"A{band:02d}" for band in range(64))
                        and dataset.nodata is not None and np.isnan(dataset.nodata)
                        and dataset.tags().get("year") == str(record["year"])
                        and dataset.tags().get("grid_id") == grid["grid_id"]
                        and dataset.tags().get("source_zarr") == str(source)
                        and dataset.tags().get("aef_export_signature") == record["export_signature"]
                        and dataset.tags().get("native_source_signature", "") == grid.get("native_source_signature", "")
                        and dataset.tags().get("native_protocol", "") == grid.get("native_protocol", "")
                        and json.loads(dataset.tags().get("native_source_manifest", "{}")) == grid.get("native_source_manifest", {})
                    )
                    if not valid:
                        errors.append(f"Invalid AEF GeoTIFF: {path.name}")
    except (OSError, ValueError, KeyError, TypeError, rasterio.errors.RasterioError) as exc:
        errors.append(f"Cannot validate GeoTIFF delivery: {exc}")
    return errors


def prepare_tiff_work(output_dir: Path, catalog_path: Path, signature: str) -> Path:
    """Claim an isolated resumable cache; never adopt a permanent Zarr delivery."""
    from .atomic import write_json

    output_dir = output_dir.resolve()
    cache = output_dir / ".aef-geotiff-work"
    if catalog_path.resolve().is_relative_to(cache):
        raise ValueError("The final TIFF catalog must be outside the temporary cache")
    marker = output_dir / ".aef-geotiff-work.json"
    expected = {"format": "geotiff", "output_root": str(output_dir), "signature": signature}
    if marker.is_symlink():
        raise ValueError("TIFF cache ownership marker must not be a link")
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != expected:
            raise ValueError("TIFF-only request differs from this output directory; choose a new directory")
    else:
        if cache.exists() or catalog_path.exists() or (output_dir / "geotiff_manifest.json").exists():
            raise FileExistsError("TIFF-only requires a new task directory; existing deliveries are not converted or deleted")
        write_json(expected, marker)
    if cache.is_symlink() or (cache.exists() and cache.resolve() != cache):
        raise ValueError("TIFF cache must not be a link")
    return cache


def finish_tiff_only(output_dir: Path, catalog_path: Path, signature: str) -> dict:
    """Publish a standalone catalog, then remove only this verified task's cache."""
    import pandas as pd
    from .atomic import write_parquet

    output_dir = output_dir.resolve()
    manifest = json.loads((output_dir / "geotiff_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "geotiff" or manifest.get("request_signature") != signature:
        raise ValueError("GeoTIFF manifest does not match the TIFF-only request")
    cache = prepare_tiff_work(output_dir, catalog_path, signature)
    grids = manifest["grids"]
    errors = validate_geotiffs(output_dir, [row["grid_id"] for row in grids], grids[0]["years"])
    if errors:
        raise ValueError("; ".join(errors))
    records = [{
        "grid_id": grid["grid_id"], "scheme": grid["scheme"], "status": "complete",
        "product_type": "aef_annual", "output_format": "geotiff", "dtype": "float32",
        "crs": grid["crs"], "transform": json.dumps(grid["transform"]),
        "width": grid["width"], "height": grid["height"], "resolution_m": grid["resolution_m"],
        "years": json.dumps(grid["years"]), "bands": json.dumps([f"A{i:02d}" for i in range(64)]),
        "source_dataset": grid["source_dataset"], "signature": grid["signature"],
        "native_protocol": grid.get("native_protocol", ""),
        "native_source_signature": grid.get("native_source_signature", ""),
        "native_source_manifest": json.dumps(grid.get("native_source_manifest", {}), sort_keys=True),
        "compression_codec": "DEFLATE", "compression_predictor": 3,
        "geotiff_paths": json.dumps([record["path"] for record in grid["files"]]),
    } for grid in grids]
    write_parquet(pd.DataFrame(records), catalog_path)
    if cache.exists():
        # Reject links before recursive cleanup; this exact directory is a task-owned cache.
        if cache.resolve().parent != output_dir or cache.name != ".aef-geotiff-work":
            raise ValueError("Unsafe TIFF cache cleanup target")
        def fail_scan(error):
            raise error

        for directory, dirs, files in os.walk(cache, followlinks=False, onerror=fail_scan):
            for entry in [Path(directory), *(Path(directory) / name for name in dirs + files)]:
                attributes = entry.lstat()
                if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("Refusing to clean a linked TIFF cache")
        shutil.rmtree(cache)
    return manifest
