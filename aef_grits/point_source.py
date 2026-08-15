"""Repeatable, bounded-memory point-source scans and exact preflight audits."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
import pandas as pd

from .earth_engine import AEF_BANDS, AEF_FIRST_YEAR, AEF_LAST_YEAR
from .points import TABLE_SUFFIXES, VECTOR_SUFFIXES, iter_vector_frame_points


GROUP_COLUMNS = ("split", "label", "species", "mgrs_tile", "grid_id", "tile_id")


def _is_geoparquet(path: Path) -> bool:
    if path.suffix.lower() != ".parquet":
        return False
    try:
        import pyarrow.parquet as pq

        metadata = pq.ParquetFile(path).schema_arrow.metadata or {}
        return b"geo" in metadata
    except Exception:
        return False


def _source_files(path: Path) -> list[Path]:
    if path.suffix.lower() != ".shp":
        return [path]
    return sorted(item for item in path.parent.glob(f"{path.stem}.*") if item.is_file())


def physical_fingerprint(path: str | Path) -> dict[str, Any]:
    """Return a path-independent size/mtime fingerprint for source components."""

    source = Path(path)
    files = _source_files(source)
    return {
        "components": [
            {
                "name": item.name,
                "size": int(item.stat().st_size),
                "mtime_ns": int(item.stat().st_mtime_ns),
            }
            for item in files
        ]
    }


@dataclass(frozen=True)
class PointSource:
    path: Path
    layer: str | None = None
    geometry_mode: str = "auto"
    id_field: str | None = None
    reference_grid: Path | None = None
    max_points: int = 1_000_000
    read_batch_size: int = 10_000

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        layer: str | None = None,
        geometry_mode: str = "auto",
        id_field: str | None = None,
        reference_grid: str | Path | None = None,
        max_points: int = 1_000_000,
        read_batch_size: int = 10_000,
    ) -> "PointSource":
        source = Path(path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in TABLE_SUFFIXES | VECTOR_SUFFIXES:
            raise ValueError(
                f"Unsupported sample format {source.suffix!r}; expected CSV, Parquet, "
                "Shapefile, GeoPackage or GeoJSON"
            )
        if max_points <= 0 or read_batch_size <= 0:
            raise ValueError("max_points and read_batch_size must be positive")
        return cls(
            path=source,
            layer=layer,
            geometry_mode=geometry_mode,
            id_field=id_field,
            reference_grid=Path(reference_grid).resolve() if reference_grid else None,
            max_points=max_points,
            read_batch_size=read_batch_size,
        )

    @property
    def is_vector(self) -> bool:
        return self.path.suffix.lower() in VECTOR_SUFFIXES or _is_geoparquet(self.path)

    def fingerprint(self) -> dict[str, Any]:
        return physical_fingerprint(self.path)

    def assert_unchanged(self, expected: dict[str, Any]) -> None:
        if self.fingerprint() != expected:
            raise ValueError("Point source changed after preflight; run validation again")

    def _iter_table_batches(self) -> Iterator[pd.DataFrame]:
        if self.path.suffix.lower() == ".csv":
            yield from pd.read_csv(
                self.path,
                chunksize=self.read_batch_size,
                low_memory=False,
            )
            return
        import pyarrow.parquet as pq

        parquet = pq.ParquetFile(self.path)
        for batch in parquet.iter_batches(batch_size=self.read_batch_size):
            yield batch.to_pandas()

    def _iter_vector_batches(self) -> Iterator[pd.DataFrame]:
        try:
            import pyogrio
        except ImportError as exc:  # pragma: no cover - download extra supplies it
            raise RuntimeError("Streaming vector input requires pyogrio") from exc

        info = pyogrio.read_info(self.path, layer=self.layer)
        total_features = int(info.get("features") or 0)
        unknown_total = total_features < 0
        batch_size = 1 if self.geometry_mode == "interior_pixels" else min(
            self.read_batch_size, 512
        )
        offset = 0
        emitted = 0
        while unknown_total or offset < total_features:
            vectors = pyogrio.read_dataframe(
                self.path,
                layer=self.layer,
                skip_features=offset,
                max_features=(
                    batch_size
                    if unknown_total
                    else min(batch_size, total_features - offset)
                ),
            )
            if vectors.empty:
                break
            vectors.index = range(offset, offset + len(vectors))
            for converted in iter_vector_frame_points(
                vectors,
                source_path=self.path,
                geometry_mode=self.geometry_mode,
                id_field=self.id_field,
                reference_grid=self.reference_grid,
                max_points=self.max_points - emitted,
                row_offset=offset,
                output_batch_size=self.read_batch_size,
            ):
                emitted += len(converted)
                if emitted > self.max_points:
                    raise ValueError(
                        f"Converted point count exceeds max_points={self.max_points:,}"
                    )
                yield converted
            offset += len(vectors)

    def _iter_normalized_batches(self) -> Iterator[pd.DataFrame]:
        batches = self._iter_vector_batches() if self.is_vector else self._iter_table_batches()
        total = 0
        for frame in batches:
            if frame.empty:
                continue
            local = frame.copy()
            if "sample_id" not in local and self.id_field and self.id_field in local:
                local["sample_id"] = local[self.id_field]
            if not {"sample_id", "lon", "lat"}.issubset(local.columns):
                raise ValueError(
                    f"Point source must normalize to sample_id, lon and lat: {self.path}"
                )
            total += len(local)
            if total > self.max_points:
                raise ValueError(
                    f"Point source exceeds max_points={self.max_points:,}; "
                    "use a smaller source or increase the explicit limit"
                )
            yield local.reset_index(drop=True)

    def iter_chunks(self, chunk_size: int) -> Iterator[pd.DataFrame]:
        """Yield deterministic normalized chunks while bounding retained rows."""

        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        pending: pd.DataFrame | None = None
        for incoming in self._iter_normalized_batches():
            position = 0
            while position < len(incoming):
                needed = chunk_size if pending is None else chunk_size - len(pending)
                take = incoming.iloc[position : position + needed]
                position += len(take)
                pending = take.copy() if pending is None else pd.concat(
                    [pending, take], ignore_index=True
                )
                if len(pending) == chunk_size:
                    yield pending.reset_index(drop=True)
                    pending = None
        if pending is not None and not pending.empty:
            yield pending.reset_index(drop=True)


def _open_audit(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-32768")
    # Append-only heaps are much faster than one indexed UPSERT per input row.
    # Exact duplicate counts are computed once with disk-backed GROUP BY.
    connection.execute("CREATE TABLE ids (value TEXT NOT NULL)")
    connection.execute("CREATE TABLE coords (value TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE groups_ (column_name TEXT NOT NULL, value TEXT NOT NULL, n INTEGER NOT NULL, "
        "PRIMARY KEY (column_name, value))"
    )
    return connection


def preflight_point_source(
    source: PointSource,
    years: Sequence[int],
    *,
    chunk_size: int,
    audit_db: str | Path,
) -> dict[str, Any]:
    """Audit a point source exactly using bounded chunks and disk-backed sets."""

    requested = sorted(set(int(year) for year in years))
    errors: list[str] = []
    warnings: list[str] = []
    if not requested:
        errors.append("At least one year is required")
    invalid_years = [
        year for year in requested if year < AEF_FIRST_YEAR or year > AEF_LAST_YEAR
    ]
    if invalid_years:
        errors.append(
            f"Years outside supported AEF range {AEF_FIRST_YEAR}-{AEF_LAST_YEAR}: {invalid_years}"
        )
    if chunk_size <= 0:
        errors.append("chunk_size must be positive")

    connection = _open_audit(Path(audit_db))
    digest = hashlib.sha256()
    content_digest = hashlib.sha256()
    rows = 0
    empty_ids = 0
    nonfinite = 0
    outside = 0
    columns: list[str] | None = None
    try:
        for frame in source.iter_chunks(max(1, chunk_size)):
            current_columns = [str(value) for value in frame.columns]
            if columns is None:
                columns = current_columns
            elif current_columns != columns:
                errors.append("Point source columns or order changed between chunks")
                break
            ids = frame.sample_id.astype(str).str.strip()
            coords = frame[["lon", "lat"]].apply(pd.to_numeric, errors="coerce")
            finite = np.isfinite(coords.to_numpy()).all(axis=1)
            in_range = (
                coords.lon.between(-180, 180, inclusive="both")
                & coords.lat.between(-90, 90, inclusive="both")
            ).to_numpy()
            empty_ids += int(ids.eq("").sum())
            nonfinite += int((~finite).sum())
            outside += int((finite & ~in_range).sum())
            id_rows = [(value,) for value in ids]
            connection.executemany(
                "INSERT INTO ids(value) VALUES (?)",
                id_rows,
            )
            coord_keys = [
                f"{lon:.12g}\t{lat:.12g}" if ok else "<invalid>"
                for lon, lat, ok in zip(coords.lon, coords.lat, finite & in_range)
            ]
            connection.executemany(
                "INSERT INTO coords(value) VALUES (?)",
                [(value,) for value in coord_keys],
            )
            for column in GROUP_COLUMNS:
                if column not in frame:
                    continue
                values = frame[column].fillna("<missing>").astype(str).value_counts()
                connection.executemany(
                    "INSERT INTO groups_(column_name,value,n) VALUES (?,?,?) "
                    "ON CONFLICT(column_name,value) DO UPDATE SET n=n+excluded.n",
                    [(column, str(value), int(count)) for value, count in values.items()],
                )
            for sample_id, lon, lat in zip(ids, coords.lon, coords.lat):
                digest.update(f"{sample_id}\t{lon:.10f}\t{lat:.10f}\n".encode())
            for values in frame.itertuples(index=False, name=None):
                normalized_values = []
                for value in values:
                    if value is None or value is pd.NA or value is pd.NaT:
                        normalized_values.append(None)
                    elif isinstance(value, np.generic):
                        item = value.item()
                        normalized_values.append(
                            None if isinstance(item, float) and math.isnan(item) else item
                        )
                    elif isinstance(value, float) and math.isnan(value):
                        normalized_values.append(None)
                    elif isinstance(value, (str, int, float, bool)):
                        normalized_values.append(value)
                    else:
                        normalized_values.append(str(value))
                content_digest.update(
                    (json.dumps(normalized_values, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                )
            rows += len(frame)
            connection.commit()

        duplicate_ids = int(
            connection.execute(
                "SELECT COALESCE(SUM(n),0) FROM "
                "(SELECT COUNT(*) AS n FROM ids GROUP BY value HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
        duplicate_coordinate_rows = int(
            connection.execute(
                "SELECT COALESCE(SUM(n),0) FROM "
                "(SELECT COUNT(*) AS n FROM coords WHERE value!='<invalid>' "
                "GROUP BY value HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
        duplicate_coordinate_groups = int(
            connection.execute(
                "SELECT COUNT(*) FROM (SELECT value FROM coords WHERE value!='<invalid>' "
                "GROUP BY value HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
        summaries: dict[str, dict[str, int]] = {}
        for column in GROUP_COLUMNS:
            values = connection.execute(
                "SELECT value,n FROM groups_ WHERE column_name=? ORDER BY n DESC,value LIMIT 100",
                (column,),
            ).fetchall()
            if values:
                summaries[column] = {str(value): int(count) for value, count in values}
    finally:
        connection.close()

    if rows == 0:
        errors.append("Point table is empty")
    if empty_ids:
        errors.append(f"Empty sample_id rows: {empty_ids}")
    if duplicate_ids:
        errors.append(f"Rows with duplicated sample_id: {duplicate_ids}")
    if nonfinite:
        errors.append(f"Rows with non-finite lon/lat: {nonfinite}")
    if outside:
        errors.append(f"Rows outside lon [-180,180] or lat [-90,90]: {outside}")
    if duplicate_coordinate_rows:
        warnings.append(
            f"{duplicate_coordinate_rows} rows share coordinates in "
            f"{duplicate_coordinate_groups} groups"
        )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "rows": rows,
        "columns": columns or [],
        "years": requested,
        "supported_year_range": [AEF_FIRST_YEAR, AEF_LAST_YEAR],
        "aef_dimensions_per_year": len(AEF_BANDS),
        "feature_count": len(requested) * len(AEF_BANDS),
        "chunk_size": int(chunk_size),
        "estimated_shards": int(math.ceil(rows / chunk_size)) if chunk_size > 0 else None,
        "duplicate_sample_id_rows": duplicate_ids,
        "empty_sample_id_rows": empty_ids,
        "duplicate_coordinate_rows": duplicate_coordinate_rows,
        "duplicate_coordinate_groups": duplicate_coordinate_groups,
        "group_counts": summaries,
        "sample_sha256": digest.hexdigest(),
        "content_sha256": content_digest.hexdigest(),
        "source_fingerprint": source.fingerprint(),
        "audit_database": str(Path(audit_db).resolve()),
        "streaming_preflight": True,
    }


def point_run_signature(
    sample_sha256: str,
    years: Sequence[int],
    *,
    scale: float,
    tile_scale: int,
    chunk_size: int,
    dataset: str,
    content_sha256: str | None = None,
) -> str:
    payload = {
        "dataset": dataset,
        "years": sorted(set(int(year) for year in years)),
        "scale": float(scale),
        "tile_scale": int(tile_scale),
        "chunk_size": int(chunk_size),
        "sample_sha256": str(sample_sha256),
    }
    if content_sha256 is not None:
        payload["content_sha256"] = str(content_sha256)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
