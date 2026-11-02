"""Read-only local AEF raster inventory; no chunk scan or native-data certification.

Run from the checkout with ``python -m
tooling.diagnostics.existing_grids_20261101.inventory --roots ... --out NEW_DIR``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat

from affine import Affine
import pandas as pd
from pyproj import CRS
import rasterio
from rasterio.warp import transform_bounds

from aef_grits.grids import MGRSGridProvider
from aef_grits.store import AEFZarr


MGRS_ID = re.compile(r"(?<![A-Z0-9])T?((?:0[1-9]|[1-5][0-9]|60)[C-HJ-NP-X][A-HJ-NP-Z]{2})(?![A-Z0-9])", re.I)
YEAR = re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})(?![0-9])")
COLUMNS = [
    "path", "grid_id", "tile_id", "format", "year", "year_evidence", "scheme",
    "crs", "transform", "width", "height", "resolution_x", "resolution_y",
    "dtype", "band_count", "protocol", "source_manifest_presence",
    "spatial_bounds_WGS84", "mgrs_candidate", "mgrs_candidate_evidence",
    "footprint_class", "mgrs_geometry_match", "resolution_class",
]


def collect_inventory(roots: list[Path]) -> tuple[pd.DataFrame, dict]:
    """Inspect metadata and the small Zarr time coordinate, never feature values."""
    discovery = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "roots": [], "errors": [], "aliases": [], "catalogs": [],
        "geotiff_manifests": [], "auxiliary": [], "pruned": [],
        "scope": "Raster headers and Zarr time only; no completeness, pixel coverage, or native-alignment pass.",
    }
    paths = _discover(roots, discovery)
    rows = []
    grids = {}
    for path in paths:
        try:
            metadata, years = _read_raster(path, discovery)
            if metadata is None:
                continue
            _describe_grid(metadata, path, grids, discovery)
            for year, evidence in years:
                rows.append({**metadata, "year": year, "year_evidence": evidence})
        except Exception as exc:
            discovery["errors"].append({"path": str(path), "stage": "raster_metadata", "error": str(exc)})
    frame = pd.DataFrame(rows, columns=COLUMNS)
    frame["year"] = pd.array(frame["year"], dtype="Int64")
    return frame, discovery


def _discover(roots: list[Path], discovery: dict) -> list[Path]:
    seen = {}
    products = []
    for requested in roots:
        root = Path(os.path.abspath(requested))
        root_record = {"path": str(root), "status": "scanned"}
        discovery["roots"].append(root_record)
        pending = [root]
        while pending:
            path = pending.pop()
            try:
                info = path.stat()
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    discovery["aliases"].append({"path": str(path), "same_as": seen[identity]})
                    if path == root:
                        root_record["status"] = "alias_already_scanned"
                    continue
                seen[identity] = str(path)
                is_directory = stat.S_ISDIR(info.st_mode)
                if is_directory and path.suffix.lower() == ".zarr":
                    products.append(path)
                    continue
                if not is_directory:
                    if path.suffix.lower() in {".tif", ".tiff"}:
                        products.append(path)
                    elif "catalog" in path.name.lower():
                        discovery["catalogs"].append(str(path))
                    elif path.name.lower() == "geotiff_manifest.json":
                        discovery["geotiff_manifests"].append(str(path))
                    elif path == root:
                        raise ValueError("Root is not a directory or supported raster/evidence file")
                    continue
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.name.lower() in {".git", ".aef_resources"}:
                            discovery["pruned"].append(entry.path)
                        elif entry.is_dir() or Path(entry.name).suffix.lower() in {".tif", ".tiff"} or (
                            "catalog" in entry.name.lower()
                            or entry.name.lower() == "geotiff_manifest.json"
                        ):
                            pending.append(Path(entry.path))
            except OSError as exc:
                root_record["status"] = "missing" if path == root and isinstance(exc, FileNotFoundError) else "scan_errors"
                discovery["errors"].append({"path": str(path), "stage": "discovery", "error": str(exc)})
            except ValueError as exc:
                root_record["status"] = "scan_errors"
                discovery["errors"].append({"path": str(path), "stage": "discovery", "error": str(exc)})
    return products


def _read_raster(path: Path, discovery: dict) -> tuple[dict | None, list]:
    if path.suffix.lower() == ".zarr":
        store = AEFZarr(path)
        attrs = dict(store.group.attrs)
        if store.array.shape != (len(store.years), 64, store.height, store.width):
            raise ValueError(f"Not an AEF [year,64,y,x] array: {store.array.shape}")
        metadata = {
            "path": str(path), "format": "zarr", "grid_id": store.grid_id,
            "tile_id": store.tile_id, "scheme": attrs.get("aef:grid_scheme", "unknown"),
            "crs": store.crs, "transform": list(store.transform)[:6],
            "width": store.width, "height": store.height,
            "dtype": str(store.array.dtype), "band_count": store.array.shape[1],
            "protocol": attrs.get("aef:native_protocol", "unknown"),
            "source_manifest_presence": bool(attrs.get("aef:native_source_manifest")),
            "_id_origin": "grid_id" if "grid_id" in attrs else "tile_id" if "tile_id" in attrs else "filename",
        }
        years = [(year, "zarr_time") for year in store.years]
        if not years:
            discovery["errors"].append({"path": str(path), "stage": "year", "error": "Empty time coordinate"})
            years = [(None, "unknown")]
    else:
        with rasterio.open(path) as source:
            if source.count != 64:
                discovery["auxiliary"].append({"path": str(path), "band_count": source.count, "reason": "not_64_band"})
                return None, []
            tags = source.tags()
            manifest = tags.get("native_source_manifest", tags.get("aef:native_source_manifest", "{}"))
            try:
                manifest_present = bool(json.loads(manifest))
            except (TypeError, ValueError):
                manifest_present = None
                discovery["errors"].append({"path": str(path), "stage": "source_manifest", "error": "Invalid JSON manifest tag"})
            metadata = {
                "path": str(path), "format": "geotiff",
                "grid_id": tags.get("grid_id", tags.get("tile_id", path.parent.name)),
                "tile_id": tags.get("tile_id", tags.get("grid_id", path.parent.name)),
                "scheme": tags.get("aef:grid_scheme", tags.get("grid_scheme", tags.get("scheme", "unknown"))),
                "crs": str(source.crs) if source.crs else None,
                "transform": list(source.transform)[:6],
                "width": source.width, "height": source.height,
                "dtype": ",".join(dict.fromkeys(source.dtypes)), "band_count": source.count,
                "protocol": tags.get("native_protocol", tags.get("aef:native_protocol", "unknown")),
                "source_manifest_presence": manifest_present,
                "_id_origin": "grid_id" if "grid_id" in tags else "tile_id" if "tile_id" in tags else "parent_directory",
            }
            raw_year = tags.get("year", tags.get("aef:year"))
            filename_years = YEAR.findall(path.stem)
            if raw_year is not None and YEAR.fullmatch(str(raw_year)):
                years = [(int(raw_year), "tiff_year_tag")]
            elif raw_year is None and len(filename_years) == 1:
                years = [(int(filename_years[0]), "annual_filename")]
            else:
                years = [(None, "unknown")]
                discovery["errors"].append({"path": str(path), "stage": "year", "error": "Missing, invalid, or ambiguous annual year"})
    return metadata, years


def _describe_grid(metadata: dict, path: Path, grids: dict, discovery: dict) -> None:
    transform = Affine(*metadata["transform"])
    x_resolution = math.hypot(transform.a, transform.d)
    y_resolution = math.hypot(transform.b, transform.e)
    metadata.update(resolution_x=x_resolution, resolution_y=y_resolution)
    crs = CRS.from_user_input(metadata["crs"]) if metadata["crs"] else None
    metre_units = bool(crs and crs.is_projected and all(
        math.isclose(axis.unit_conversion_factor, 1.0) for axis in crs.axis_info[:2]
    ))
    if metre_units and math.isclose(x_resolution, 10) and math.isclose(y_resolution, 10):
        metadata["resolution_class"] = "10m_grid_not_native_verified"
    elif metre_units and math.isclose(x_resolution, 30) and math.isclose(y_resolution, 30):
        metadata["resolution_class"] = "30m_derivative_not_native_10m"
    else:
        metadata["resolution_class"] = "other_or_unknown"
    if crs:
        corners = [transform * (column, row) for column, row in (
            (0, 0), (metadata["width"], 0), (0, metadata["height"]),
            (metadata["width"], metadata["height"]),
        )]
        xs, ys = zip(*corners)
        bounds = (min(xs), min(ys), max(xs), max(ys))
        metadata["spatial_bounds_WGS84"] = json.dumps(transform_bounds(crs, "EPSG:4326", *bounds))
    else:
        metadata["spatial_bounds_WGS84"] = None
        discovery["errors"].append({"path": str(path), "stage": "grid", "error": "Missing CRS"})
    candidate = None
    for origin, value in (
        (metadata.pop("_id_origin"), metadata["grid_id"]), ("tile_id", metadata["tile_id"]),
        ("filename", path.stem), ("parent_directory", path.parent.name),
    ):
        match = MGRS_ID.search(str(value))
        if match:
            candidate = match.group(1).upper()
            metadata["mgrs_candidate_evidence"] = origin + (":exact" if MGRS_ID.fullmatch(str(value)) else ":derived_name")
            break
    metadata["mgrs_candidate"] = candidate
    metadata["mgrs_geometry_match"] = False
    metadata["footprint_class"] = "mgrs_subset/reference" if candidate else "other"
    if candidate:
        if candidate not in grids:
            try:
                grids[candidate] = MGRSGridProvider().get(candidate)
            except Exception as exc:
                grids[candidate] = None
                discovery["errors"].append({"path": str(path), "stage": "mgrs_index", "error": str(exc)})
        grid = grids[candidate]
        if grid is not None:
            matches = bool(
                crs == CRS.from_user_input(grid.crs)
                and metadata["width"] == grid.width and metadata["height"] == grid.height
                and all(math.isclose(a, b, rel_tol=0, abs_tol=1e-6) for a, b in zip(transform, grid.transform))
            )
            metadata["mgrs_geometry_match"] = matches
            if matches:
                metadata["footprint_class"] = "full_mgrs"
    metadata["transform"] = json.dumps(metadata["transform"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True, help="New report directory; never overwritten")
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error(f"Report directory already exists: {args.out}")
    frame, discovery = collect_inventory(args.roots)
    args.out.mkdir(parents=True, exist_ok=False)
    frame.to_csv(args.out / "inventory.csv", index=False, encoding="utf-8-sig")
    frame.to_parquet(args.out / "inventory.parquet", index=False)
    (args.out / "discovery.json").write_text(json.dumps(discovery, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(frame), "products": frame.path.nunique(), "errors": len(discovery["errors"]), "out": str(args.out.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
