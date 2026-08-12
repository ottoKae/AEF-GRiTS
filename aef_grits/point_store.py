"""Validated loading of directly streamed AEF point Parquet shards."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Sequence

import pandas as pd

from .earth_engine import feature_columns


YEAR_PATTERN = re.compile(r"^aef(\d{4})_center_A\d{2}$")


@dataclass(frozen=True)
class AEFPointDataset:
    frame: pd.DataFrame
    report: dict


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
