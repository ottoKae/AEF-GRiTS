"""Point-source conversion and preflight validation for AEF extraction."""

from __future__ import annotations

from collections.abc import Sequence
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .earth_engine import AEF_BANDS, AEF_FIRST_YEAR, AEF_LAST_YEAR


TABLE_SUFFIXES = {".csv", ".parquet"}
VECTOR_SUFFIXES = {".shp", ".gpkg", ".geojson", ".json"}


def _read_plain_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, low_memory=False)


def _base_identifier(row: pd.Series, row_number: int, id_field: str | None) -> str:
    field = id_field or ("sample_id" if "sample_id" in row.index else None)
    if field is not None:
        if field not in row.index:
            raise ValueError(f"ID field is absent from vector source: {field}")
        value = row[field]
        if pd.isna(value) or not str(value).strip():
            raise ValueError(f"Empty ID at source row {row_number}: {field}")
        return str(value)
    return f"feature_{row_number:08d}"


def _reference_contract(path: str | Path) -> tuple[str, Any, int, int]:
    """Return CRS, affine, width and height from a GeoTIFF or AEF Zarr."""
    source = Path(path)
    if source.suffix.lower() == ".zarr" or source.name.lower().endswith(".zarr"):
        import zarr
        from affine import Affine

        group = zarr.open_group(str(source), mode="r")
        return (
            str(group.attrs["crs"]),
            Affine(*group.attrs["transform"]),
            int(group.attrs["width"]),
            int(group.attrs["height"]),
        )
    import rasterio

    with rasterio.open(source) as dataset:
        if dataset.crs is None:
            raise ValueError(f"Reference grid has no CRS: {source}")
        return str(dataset.crs), dataset.transform, dataset.width, dataset.height


def _iter_interior_pixel_centres(
    geometry,
    transform,
    width: int,
    height: int,
    max_points: int,
    *,
    batch_points: int = 10_000,
):
    """Yield bounded arrays of reference-grid centres inside one geometry."""
    from rasterio.windows import from_bounds
    from shapely import contains_xy

    window = from_bounds(*geometry.bounds, transform=transform)
    row0 = max(0, int(math.floor(window.row_off)))
    row1 = min(height, int(math.ceil(window.row_off + window.height)))
    col0 = max(0, int(math.floor(window.col_off)))
    col1 = min(width, int(math.ceil(window.col_off + window.width)))
    if row0 >= row1 or col0 >= col1:
        return
    column_values = np.arange(col0, col1, dtype=np.int64)
    rows_per_batch = max(1, int(batch_points) // max(1, len(column_values)))
    selected_count = 0
    for batch_row0 in range(row0, row1, rows_per_batch):
        batch_row1 = min(row1, batch_row0 + rows_per_batch)
        rows, columns = np.meshgrid(
            np.arange(batch_row0, batch_row1, dtype=np.int64),
            column_values,
            indexing="ij",
        )
        columns_center = columns.ravel() + 0.5
        rows_center = rows.ravel() + 0.5
        xs = (
            transform.a * columns_center
            + transform.b * rows_center
            + transform.c
        )
        ys = (
            transform.d * columns_center
            + transform.e * rows_center
            + transform.f
        )
        inside = contains_xy(geometry, xs, ys)
        batch = np.column_stack([xs[inside], ys[inside]])
        selected_count += len(batch)
        if selected_count > max_points:
            raise ValueError(
                f"Polygon interior exceeds remaining max_points={max_points:,}"
            )
        if len(batch):
            yield batch


def _interior_pixel_centres(
    geometry, transform, width: int, height: int, max_points: int
):
    """Return all centres for the backward-compatible in-memory API."""
    selected = list(
        _iter_interior_pixel_centres(
            geometry, transform, width, height, max_points
        )
    )
    return np.concatenate(selected) if selected else np.empty((0, 2), dtype=np.float64)


def vector_frame_to_points(
    vectors,
    *,
    source_path: str | Path,
    layer: str | None = None,
    geometry_mode: str = "auto",
    id_field: str | None = None,
    reference_grid: str | Path | None = None,
    max_points: int = 1_000_000,
    row_offset: int = 0,
) -> pd.DataFrame:
    """Convert one already loaded vector batch into a WGS84 point table.

    ``auto`` keeps point features and uses one representative interior point for
    polygons. ``representative`` and ``centroid`` force the corresponding
    polygon rule. ``interior_pixels`` emits every reference-grid pixel centre
    strictly inside each polygon and therefore requires ``reference_grid``.
    """
    import geopandas as gpd
    from shapely.geometry import MultiPoint, Point

    source = Path(source_path)
    if vectors.empty:
        raise ValueError(f"Vector source is empty: {source}")
    if vectors.crs is None:
        raise ValueError(f"Vector source has no CRS: {source}")
    if geometry_mode not in {"auto", "representative", "centroid", "interior_pixels"}:
        raise ValueError(f"Unsupported geometry mode: {geometry_mode}")
    if max_points <= 0:
        raise ValueError("max_points must be positive")

    reference = None
    projected = vectors
    if geometry_mode == "interior_pixels":
        if reference_grid is None:
            raise ValueError("interior_pixels requires --reference-grid")
        reference = _reference_contract(reference_grid)
        projected = vectors.to_crs(reference[0])

    records: list[dict[str, Any]] = []
    for local_position, ((source_index, row), (_, projected_row)) in enumerate(
        zip(vectors.iterrows(), projected.iterrows()), 1
    ):
        position = row_offset + local_position
        geometry = projected_row.geometry
        if geometry is None or geometry.is_empty:
            continue
        attributes = row.drop(labels=[vectors.geometry.name]).to_dict()
        base_id = _base_identifier(row, position, id_field)
        geometry_type = geometry.geom_type
        point_items: list[tuple[Any, str]] = []
        if isinstance(geometry, Point):
            point_items = [(geometry, "source_point")]
        elif isinstance(geometry, MultiPoint):
            point_items = [(point, "source_point") for point in geometry.geoms]
        elif "Polygon" in geometry_type:
            if geometry_mode in {"auto", "representative"}:
                point_items = [(geometry.representative_point(), "representative_point")]
            elif geometry_mode == "centroid":
                point_items = [(geometry.centroid, "centroid")]
            else:
                crs, transform, width, height = reference
                xy = _interior_pixel_centres(
                    geometry,
                    transform,
                    width,
                    height,
                    max_points=max_points - len(records),
                )
                point_items = [(Point(x, y), "interior_pixel") for x, y in xy]
        else:
            raise ValueError(
                f"Unsupported geometry at source row {position}: {geometry_type}"
            )
        if len(records) + len(point_items) > max_points:
            raise ValueError(
                f"Converted point count would exceed max_points={max_points:,}; "
                "use a smaller source or increase --max-points explicitly"
            )
        point_series = gpd.GeoSeries(
            [item[0] for item in point_items], crs=projected.crs
        ).to_crs("EPSG:4326")
        multiple = len(point_items) != 1
        for point_index, (point, (_, method)) in enumerate(zip(point_series, point_items), 1):
            record = dict(attributes)
            record.update(
                {
                    "sample_id": (
                        f"{base_id}__px{point_index:06d}" if multiple else base_id
                    ),
                    "lon": float(point.x),
                    "lat": float(point.y),
                    "source_feature_id": str(source_index),
                    "source_geometry_type": geometry_type,
                    "point_method": method,
                    "source_path": str(source.resolve()),
                }
            )
            records.append(record)
    if not records:
        raise ValueError(f"No usable point or polygon geometry in {source}")
    return pd.DataFrame.from_records(records)


def iter_vector_frame_points(
    vectors,
    *,
    source_path: str | Path,
    geometry_mode: str = "auto",
    id_field: str | None = None,
    reference_grid: str | Path | None = None,
    max_points: int = 1_000_000,
    row_offset: int = 0,
    output_batch_size: int = 10_000,
):
    """Yield bounded normalized batches from an already loaded vector batch."""
    import geopandas as gpd
    from shapely.geometry import MultiPoint, Point

    source = Path(source_path)
    if vectors.empty:
        return
    if vectors.crs is None:
        raise ValueError(f"Vector source has no CRS: {source}")
    if geometry_mode not in {"auto", "representative", "centroid", "interior_pixels"}:
        raise ValueError(f"Unsupported geometry mode: {geometry_mode}")
    if max_points <= 0 or output_batch_size <= 0:
        raise ValueError("max_points and output_batch_size must be positive")
    reference = None
    projected = vectors
    if geometry_mode == "interior_pixels":
        if reference_grid is None:
            raise ValueError("interior_pixels requires --reference-grid")
        reference = _reference_contract(reference_grid)
        projected = vectors.to_crs(reference[0])

    records: list[dict[str, Any]] = []
    emitted = 0

    def flush(force: bool = False):
        nonlocal records
        while len(records) >= output_batch_size or (force and records):
            count = output_batch_size if len(records) >= output_batch_size else len(records)
            batch = records[:count]
            records = records[count:]
            yield pd.DataFrame.from_records(batch)

    for local_position, ((source_index, row), (_, projected_row)) in enumerate(
        zip(vectors.iterrows(), projected.iterrows()), 1
    ):
        position = row_offset + local_position
        geometry = projected_row.geometry
        if geometry is None or geometry.is_empty:
            continue
        attributes = row.drop(labels=[vectors.geometry.name]).to_dict()
        base_id = _base_identifier(row, position, id_field)
        geometry_type = geometry.geom_type
        if isinstance(geometry, Point):
            item_batches = [([geometry], "source_point", False)]
        elif isinstance(geometry, MultiPoint):
            item_batches = [(list(geometry.geoms), "source_point", len(geometry.geoms) != 1)]
        elif "Polygon" in geometry_type and geometry_mode in {"auto", "representative"}:
            item_batches = [([geometry.representative_point()], "representative_point", False)]
        elif "Polygon" in geometry_type and geometry_mode == "centroid":
            item_batches = [([geometry.centroid], "centroid", False)]
        elif "Polygon" in geometry_type and geometry_mode == "interior_pixels":
            crs, transform, width, height = reference
            item_batches = (
                ([Point(x, y) for x, y in xy], "interior_pixel", True)
                for xy in _iter_interior_pixel_centres(
                    geometry,
                    transform,
                    width,
                    height,
                    max_points=max_points - emitted,
                    batch_points=output_batch_size,
                )
            )
        else:
            raise ValueError(
                f"Unsupported geometry at source row {position}: {geometry_type}"
            )

        point_index = 0
        for raw_points, method, multiple in item_batches:
            if not raw_points:
                continue
            point_series = gpd.GeoSeries(raw_points, crs=projected.crs).to_crs("EPSG:4326")
            for point in point_series:
                point_index += 1
                emitted += 1
                if emitted > max_points:
                    raise ValueError(
                        f"Converted point count exceeds max_points={max_points:,}"
                    )
                record = dict(attributes)
                record.update(
                    {
                        "sample_id": (
                            f"{base_id}__px{point_index:06d}" if multiple else base_id
                        ),
                        "lon": float(point.x),
                        "lat": float(point.y),
                        "source_feature_id": str(source_index),
                        "source_geometry_type": geometry_type,
                        "point_method": method,
                        "source_path": str(source.resolve()),
                    }
                )
                records.append(record)
                yield from flush()
    yield from flush(force=True)


def vector_to_points(
    path: str | Path,
    *,
    layer: str | None = None,
    geometry_mode: str = "auto",
    id_field: str | None = None,
    reference_grid: str | Path | None = None,
    max_points: int = 1_000_000,
) -> pd.DataFrame:
    """Convert point or polygon vector data into a WGS84 point table."""
    import geopandas as gpd

    source = Path(path)
    if source.suffix.lower() == ".parquet":
        vectors = gpd.read_parquet(source)
    else:
        vectors = gpd.read_file(source, layer=layer)
    return vector_frame_to_points(
        vectors,
        source_path=source,
        layer=layer,
        geometry_mode=geometry_mode,
        id_field=id_field,
        reference_grid=reference_grid,
        max_points=max_points,
    )


def load_point_source(
    path: str | Path,
    *,
    layer: str | None = None,
    geometry_mode: str = "auto",
    id_field: str | None = None,
    reference_grid: str | Path | None = None,
    max_points: int = 1_000_000,
) -> pd.DataFrame:
    """Read CSV/Parquet coordinates or convert a supported vector source."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in TABLE_SUFFIXES | VECTOR_SUFFIXES:
        raise ValueError(
            f"Unsupported sample format {suffix!r}; expected CSV, Parquet, "
            "Shapefile, GeoPackage or GeoJSON"
        )
    if suffix in TABLE_SUFFIXES:
        table = _read_plain_table(source)
        if {"lon", "lat"}.issubset(table.columns):
            return table
        if suffix == ".parquet" and "geometry" in table.columns:
            return vector_to_points(
                source,
                layer=layer,
                geometry_mode=geometry_mode,
                id_field=id_field,
                reference_grid=reference_grid,
                max_points=max_points,
            )
        raise ValueError(
            f"Coordinate table must contain lon and lat columns: {source}"
        )
    return vector_to_points(
        source,
        layer=layer,
        geometry_mode=geometry_mode,
        id_field=id_field,
        reference_grid=reference_grid,
        max_points=max_points,
    )


def validate_point_table(
    frame: pd.DataFrame,
    years: Sequence[int],
    *,
    chunk_size: int = 1_000,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Normalize a point table and return a JSON-ready preflight report."""
    errors: list[str] = []
    warnings: list[str] = []
    normalized = frame.copy().reset_index(drop=True)
    requested = sorted(set(int(year) for year in years))
    required = {"sample_id", "lon", "lat"}
    missing = sorted(required.difference(normalized.columns))
    if missing:
        errors.append(f"Missing required columns: {missing}")
    if normalized.empty:
        errors.append("Point table is empty")
    if chunk_size <= 0:
        errors.append("chunk_size must be positive")
    if not requested:
        errors.append("At least one year is required")
    invalid_years = [
        year for year in requested if year < AEF_FIRST_YEAR or year > AEF_LAST_YEAR
    ]
    if invalid_years:
        errors.append(
            f"Years outside supported AEF range {AEF_FIRST_YEAR}-{AEF_LAST_YEAR}: "
            f"{invalid_years}"
        )

    duplicate_ids = 0
    empty_ids = 0
    duplicate_coordinate_rows = 0
    duplicate_coordinate_groups = 0
    if not missing:
        normalized["sample_id"] = normalized.sample_id.astype(str).str.strip()
        empty_ids = int(normalized.sample_id.eq("").sum())
        duplicate_ids = int(normalized.sample_id.duplicated(keep=False).sum())
        coords = normalized[["lon", "lat"]].apply(pd.to_numeric, errors="coerce")
        finite = np.isfinite(coords.to_numpy()).all(axis=1)
        in_range = (
            coords.lon.between(-180.0, 180.0, inclusive="both")
            & coords.lat.between(-90.0, 90.0, inclusive="both")
        ).to_numpy()
        if empty_ids:
            errors.append(f"Empty sample_id rows: {empty_ids}")
        if duplicate_ids:
            errors.append(f"Rows with duplicated sample_id: {duplicate_ids}")
        if not finite.all():
            errors.append(f"Rows with non-finite lon/lat: {int((~finite).sum())}")
        if not (finite & in_range).all():
            errors.append(
                "Rows outside lon [-180,180] or lat [-90,90]: "
                f"{int((finite & ~in_range).sum())}"
            )
        normalized[["lon", "lat"]] = coords
        valid_coords = normalized.loc[finite & in_range, ["lon", "lat"]]
        duplicated = valid_coords.duplicated(["lon", "lat"], keep=False)
        duplicate_coordinate_rows = int(duplicated.sum())
        duplicate_coordinate_groups = int(
            valid_coords.loc[duplicated].drop_duplicates().shape[0]
        )
        if duplicate_coordinate_rows:
            warnings.append(
                f"{duplicate_coordinate_rows} rows share coordinates in "
                f"{duplicate_coordinate_groups} groups"
            )

    summaries = {}
    for column in ("split", "label", "species", "mgrs_tile", "grid_id", "tile_id"):
        if column in normalized:
            summaries[column] = {
                str(key): int(value)
                for key, value in normalized[column]
                .fillna("<missing>")
                .astype(str)
                .value_counts(dropna=False)
                .head(100)
                .items()
            }
    report = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "rows": int(len(normalized)),
        "columns": [str(column) for column in normalized.columns],
        "years": requested,
        "supported_year_range": [AEF_FIRST_YEAR, AEF_LAST_YEAR],
        "aef_dimensions_per_year": len(AEF_BANDS),
        "feature_count": len(requested) * len(AEF_BANDS),
        "chunk_size": int(chunk_size),
        "estimated_shards": (
            int(math.ceil(len(normalized) / chunk_size)) if chunk_size > 0 else None
        ),
        "duplicate_sample_id_rows": duplicate_ids,
        "empty_sample_id_rows": empty_ids,
        "duplicate_coordinate_rows": duplicate_coordinate_rows,
        "duplicate_coordinate_groups": duplicate_coordinate_groups,
        "group_counts": summaries,
    }
    return normalized, report


def raise_for_invalid_points(report: dict[str, Any]) -> None:
    if not report["valid"]:
        raise ValueError("Point preflight failed: " + "; ".join(report["errors"]))
