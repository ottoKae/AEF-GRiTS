"""Validated loading of directly streamed AEF point Parquet shards."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Sequence

import pandas as pd

from .earth_engine import feature_columns


YEAR_PATTERN = re.compile(r"^aef(\d{4})_center_A\d{2}$")


@dataclass(frozen=True)
class AEFPointDataset:
    frame: pd.DataFrame
    report: dict


@dataclass(frozen=True)
class AEFPointArrowDataset:
    """Validated out-of-core access to catalogued point Parquet shards."""

    catalog_path: Path
    paths: tuple[Path, ...]
    dataset: object
    available_years: tuple[int, ...]
    requested_years: tuple[int, ...]
    report: dict

    @property
    def columns(self) -> list[str]:
        return [str(name) for name in self.dataset.schema.names]

    def iter_batches(
        self,
        *,
        columns: Sequence[str] | None = None,
        years: Sequence[int] | None = None,
        batch_size: int = 4096,
        filter=None,
        as_pandas: bool = True,
    ):
        """Yield projected Arrow record batches or bounded Pandas frames."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        selected = list(columns) if columns is not None else self.columns
        if years is not None:
            for name in feature_columns(sorted(set(int(year) for year in years))):
                if name not in selected:
                    selected.append(name)
        missing = sorted(set(selected).difference(self.columns))
        if missing:
            raise ValueError(f"Requested point columns are absent: {missing[:5]}")
        scanner = self.dataset.scanner(
            columns=selected,
            filter=filter,
            batch_size=batch_size,
            use_threads=True,
        )
        for batch in scanner.to_batches():
            yield batch.to_pandas() if as_pandas else batch


def _catalog_path(path: str | Path) -> Path:
    source = Path(path)
    return source / "catalog.parquet" if source.is_dir() else source


def _discover_years(columns: Sequence[str]) -> list[int]:
    return sorted(
        {
            int(match.group(1))
            for column in columns
            if (match := YEAR_PATTERN.match(str(column)))
        }
    )


def _resolve_catalog_shards(catalog_path: Path, catalog: pd.DataFrame) -> list[Path]:
    paths = []
    for row in catalog.sort_values("chunk" if "chunk" in catalog else "path").itertuples(
        index=False
    ):
        path = Path(row.path)
        if not path.is_absolute():
            path = catalog_path.parent / path
        if not path.exists():
            raise FileNotFoundError(f"Point shard is missing: {path}")
        paths.append(path.resolve())
    return paths


def _audit_duplicate_ids(dataset, audit_db: Path, batch_size: int) -> int:
    audit_db.parent.mkdir(parents=True, exist_ok=True)
    audit_db.unlink(missing_ok=True)
    connection = sqlite3.connect(audit_db)
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-32768")
    connection.execute("CREATE TABLE ids(value TEXT NOT NULL)")
    try:
        for batch in dataset.scanner(columns=["sample_id"], batch_size=batch_size).to_batches():
            values = batch.column(0).to_pylist()
            connection.executemany(
                "INSERT INTO ids(value) VALUES (?)",
                [(str(value),) for value in values],
            )
            connection.commit()
        return int(
            connection.execute(
                "SELECT COALESCE(SUM(n),0) FROM "
                "(SELECT COUNT(*) AS n FROM ids GROUP BY value HAVING COUNT(*)>1)"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def open_aef_point_dataset(
    catalog_or_directory: str | Path,
    *,
    years: Sequence[int] | None = None,
    require_complete: bool = False,
    check_duplicate_ids: bool = False,
    audit_db: str | Path | None = None,
    batch_size: int = 4096,
) -> AEFPointArrowDataset:
    """Open and validate point shards without concatenating them into memory."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    import pyarrow.dataset as ds
    import pyarrow.parquet as pq

    catalog_path = _catalog_path(catalog_or_directory).resolve()
    catalog = pd.read_parquet(catalog_path)
    missing = {"path", "rows"}.difference(catalog.columns)
    if missing:
        raise ValueError(f"Point catalog missing columns: {sorted(missing)}")
    if catalog.empty:
        raise ValueError(f"Point catalog is empty: {catalog_path}")
    if "signature" in catalog and catalog.signature.astype(str).nunique() != 1:
        raise ValueError("Point catalog mixes multiple run signatures")
    paths = _resolve_catalog_shards(catalog_path, catalog)
    expected_columns = None
    total_rows = 0
    for path, row in zip(paths, catalog.sort_values("chunk" if "chunk" in catalog else "path").itertuples(index=False)):
        parquet = pq.ParquetFile(path)
        actual_rows = int(parquet.metadata.num_rows)
        if actual_rows != int(row.rows):
            raise ValueError(
                f"Point shard row mismatch: {path}: catalog={row.rows}, actual={actual_rows}"
            )
        columns = tuple(parquet.schema_arrow.names)
        if expected_columns is None:
            expected_columns = columns
        elif columns != expected_columns:
            raise ValueError(f"Point shard schema drift detected: {path}")
        total_rows += actual_rows
    if expected_columns is None or "sample_id" not in expected_columns:
        raise ValueError("Point shards have no sample_id")
    available = _discover_years(expected_columns)
    requested = available if years is None else sorted(set(int(year) for year in years))
    missing_years = sorted(set(requested).difference(available))
    if missing_years:
        raise ValueError(
            f"Requested years are absent from point shards: {missing_years}; available={available}"
        )
    selected_features = feature_columns(requested)
    missing_features = [name for name in selected_features if name not in expected_columns]
    if missing_features:
        raise ValueError(
            f"Point shards have an incomplete AEF schema; examples={missing_features[:3]}"
        )
    arrow_dataset = ds.dataset([str(path) for path in paths], format="parquet")
    complete_rows = 0
    annual_complete = {year: 0 for year in requested}
    for batch in arrow_dataset.scanner(
        columns=selected_features,
        batch_size=batch_size,
        use_threads=True,
    ).to_batches():
        frame = batch.to_pandas()
        all_complete = frame.notna().all(axis=1)
        complete_rows += int(all_complete.sum())
        for year in requested:
            annual_columns = feature_columns([year])
            annual_complete[year] += int(frame[annual_columns].notna().all(axis=1).sum())
    duplicate_rows = None
    if check_duplicate_ids:
        if audit_db is None:
            raise ValueError("check_duplicate_ids requires audit_db on a native filesystem")
        duplicate_rows = _audit_duplicate_ids(arrow_dataset, Path(audit_db), batch_size)
        if duplicate_rows:
            raise ValueError(f"Point shards contain {duplicate_rows} duplicated sample_id rows")
    report = {
        "catalog": str(catalog_path),
        "shards": len(paths),
        "rows": total_rows,
        "available_years": available,
        "requested_years": requested,
        "feature_count": len(selected_features),
        "complete_rows": complete_rows,
        "incomplete_rows": total_rows - complete_rows,
        "complete_fraction": complete_rows / total_rows if total_rows else 0.0,
        "annual_completeness": {
            str(year): {
                "complete_rows": annual_complete[year],
                "incomplete_rows": total_rows - annual_complete[year],
                "complete_fraction": annual_complete[year] / total_rows if total_rows else 0.0,
            }
            for year in requested
        },
        "signature": str(catalog.signature.iloc[0]) if "signature" in catalog else None,
        "duplicate_sample_id_rows": duplicate_rows,
        "out_of_core": True,
    }
    if require_complete and complete_rows != total_rows:
        raise ValueError(
            f"Point dataset is incomplete: {total_rows - complete_rows} of {total_rows} rows"
        )
    return AEFPointArrowDataset(
        catalog_path=catalog_path,
        paths=tuple(paths),
        dataset=arrow_dataset,
        available_years=tuple(available),
        requested_years=tuple(requested),
        report=report,
    )


def iter_aef_point_batches(
    catalog_or_directory: str | Path,
    *,
    years: Sequence[int] | None = None,
    columns: Sequence[str] | None = None,
    batch_size: int = 4096,
    filter=None,
):
    """Convenience iterator over validated, projected Pandas batches."""
    store = open_aef_point_dataset(
        catalog_or_directory,
        years=years,
        batch_size=batch_size,
    )
    yield from store.iter_batches(
        columns=columns,
        years=years,
        batch_size=batch_size,
        filter=filter,
        as_pandas=True,
    )


def load_aef_points(
    catalog_or_directory: str | Path,
    *,
    years: Sequence[int] | None = None,
    require_complete: bool = False,
) -> AEFPointDataset:
    """Load all catalogued point shards and enforce their integrity contract."""
    catalog_path = _catalog_path(catalog_or_directory)
    catalog = pd.read_parquet(catalog_path)
    required = {"path", "rows"}
    missing = required.difference(catalog.columns)
    if missing:
        raise ValueError(f"Point catalog missing columns: {sorted(missing)}")
    if catalog.empty:
        raise ValueError(f"Point catalog is empty: {catalog_path}")
    if "signature" in catalog and catalog.signature.astype(str).nunique() != 1:
        raise ValueError("Point catalog mixes multiple run signatures")
    order = "chunk" if "chunk" in catalog else "path"
    frames = []
    for row in catalog.sort_values(order).itertuples(index=False):
        path = Path(row.path)
        if not path.is_absolute():
            path = catalog_path.parent / path
        if not path.exists():
            raise FileNotFoundError(f"Point shard is missing: {path}")
        shard = pd.read_parquet(path)
        if len(shard) != int(row.rows):
            raise ValueError(
                f"Point shard row mismatch: {path}: catalog={row.rows}, actual={len(shard)}"
            )
        frames.append(shard)
    frame = pd.concat(frames, ignore_index=True)
    if "sample_id" not in frame:
        raise ValueError("Point shards have no sample_id")
    frame["sample_id"] = frame.sample_id.astype(str)
    duplicated = int(frame.sample_id.duplicated(keep=False).sum())
    if duplicated:
        raise ValueError(f"Point shards contain {duplicated} duplicated sample_id rows")

    available = _discover_years(frame.columns)
    requested = available if years is None else sorted(set(int(year) for year in years))
    missing_years = sorted(set(requested).difference(available))
    if missing_years:
        raise ValueError(
            f"Requested years are absent from point shards: {missing_years}; "
            f"available={available}"
        )
    missing_features = [name for name in feature_columns(requested) if name not in frame]
    if missing_features:
        raise ValueError(
            f"Point shards have an incomplete AEF schema; examples={missing_features[:3]}"
        )
    completeness = {}
    for year in requested:
        columns = feature_columns([year])
        complete = frame[columns].notna().all(axis=1)
        completeness[str(year)] = {
            "complete_rows": int(complete.sum()),
            "incomplete_rows": int((~complete).sum()),
            "complete_fraction": float(complete.mean()),
        }
    selected_columns = feature_columns(requested)
    complete_all = frame[selected_columns].notna().all(axis=1)
    report = {
        "catalog": str(catalog_path.resolve()),
        "shards": int(len(frames)),
        "rows": int(len(frame)),
        "unique_sample_ids": int(frame.sample_id.nunique()),
        "available_years": available,
        "requested_years": requested,
        "feature_count": len(selected_columns),
        "complete_rows": int(complete_all.sum()),
        "incomplete_rows": int((~complete_all).sum()),
        "complete_fraction": float(complete_all.mean()),
        "annual_completeness": completeness,
        "signature": (
            str(catalog.signature.iloc[0]) if "signature" in catalog else None
        ),
    }
    if require_complete and not bool(complete_all.all()):
        raise ValueError(
            f"Point dataset is incomplete: {report['incomplete_rows']} of {len(frame)} rows"
        )
    return AEFPointDataset(frame=frame, report=report)
