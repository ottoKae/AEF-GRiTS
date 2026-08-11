"""Numerical AlphaEarth helpers localized from ``AEF_DUDU/helper``.

AlphaEarth Foundation (AEF) vectors live on a useful cosine geometry.  This
module intentionally keeps the helper project's strict 64-D contract, L2
normalization, spherical prototypes and angular comparison while extending it
to ordered multi-year windows.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd


AEF_DIMENSIONS = 64
AEF_COLUMN_RE = re.compile(
    r"^aef(?P<year>\d{4})_(?P<spatial>[A-Za-z0-9]+)_A(?P<band>\d{2})$"
)


@dataclass(frozen=True)
class FeatureQC:
    rows: int
    columns: int
    missing_values: int
    nonfinite_values: int
    zero_norm_rows: int
    valid_rows: int

    def to_dict(self) -> dict:
        return asdict(self)


def feature_columns(year: int, spatial_type: str = "center") -> list[str]:
    return [f"aef{int(year)}_{spatial_type}_A{i:02d}" for i in range(AEF_DIMENSIONS)]


def discover_years(columns: Iterable[str], spatial_type: str = "center") -> list[int]:
    groups: dict[int, set[int]] = {}
    for column in columns:
        match = AEF_COLUMN_RE.match(str(column))
        if match and match.group("spatial") == spatial_type:
            groups.setdefault(int(match.group("year")), set()).add(int(match.group("band")))
    expected = set(range(AEF_DIMENSIONS))
    return sorted(year for year, bands in groups.items() if bands == expected)


def feature_qc(values: np.ndarray) -> FeatureQC:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("AEF values must be a 2-D matrix")
    finite_rows = np.isfinite(values).all(axis=1)
    safe = np.where(np.isfinite(values), values, 0.0)
    zero = np.isclose(np.linalg.norm(safe, axis=1), 0.0)
    return FeatureQC(
        rows=int(values.shape[0]),
        columns=int(values.shape[1]),
        missing_values=int(np.isnan(values).sum()),
        nonfinite_values=int((~np.isfinite(values)).sum()),
        zero_norm_rows=int(zero.sum()),
        valid_rows=int((finite_rows & ~zero).sum()),
    )


def valid_feature_rows(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    return np.isfinite(values).all(axis=1) & ~np.isclose(np.linalg.norm(values, axis=1), 0.0)


def l2_normalize(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    if not np.isfinite(values).all():
        raise ValueError("Cannot normalize non-finite AEF vectors")
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if np.any(np.isclose(norms, 0.0)):
        raise ValueError("Cannot normalize zero-norm AEF vectors")
    return values / norms


def spherical_prototype(values: np.ndarray) -> np.ndarray:
    center = l2_normalize(values).mean(axis=0, keepdims=True)
    return l2_normalize(center)[0]


def cosine_to_prototype(values: np.ndarray, prototype: np.ndarray) -> np.ndarray:
    unit = l2_normalize(values)
    proto = l2_normalize(np.asarray(prototype, dtype=np.float64).reshape(1, -1))[0]
    if unit.shape[1] != len(proto):
        raise ValueError("Feature and prototype dimensions differ")
    return np.clip(unit @ proto, -1.0, 1.0)


def prototype_angle_degrees(first: np.ndarray, second: np.ndarray) -> float:
    first_u = l2_normalize(np.asarray(first).reshape(1, -1))[0]
    second_u = l2_normalize(np.asarray(second).reshape(1, -1))[0]
    return float(np.degrees(np.arccos(np.clip(first_u @ second_u, -1.0, 1.0))))


def _linear_slope(sequence: np.ndarray) -> np.ndarray:
    """Least-squares slope over the ordered year axis, vectorized by feature."""
    n_years = sequence.shape[1]
    if n_years == 1:
        return np.zeros((sequence.shape[0], sequence.shape[2]), dtype=np.float64)
    time = np.arange(n_years, dtype=np.float64)
    centered = time - time.mean()
    return np.einsum("ntd,t->nd", sequence, centered) / np.sum(centered ** 2)


def fuse_years(sequence: np.ndarray, mode: str = "concat") -> tuple[np.ndarray, list[str]]:
    """Fuse an ``[N,T,64]`` ordered AEF sequence.

    ``concat`` is the direct baseline. ``temporal_stats`` is a deliberately
    small order-aware diagnostic: mean, standard deviation, last-first change,
    and linear slope.  It is not presented as a replacement for a learned
    temporal encoder.
    """
    sequence = np.asarray(sequence, dtype=np.float64)
    if sequence.ndim != 3 or sequence.shape[2] != AEF_DIMENSIONS:
        raise ValueError("AEF sequence must have shape [N,T,64]")
    if mode == "concat":
        names = [f"t{t}_A{band:02d}" for t in range(sequence.shape[1])
                 for band in range(AEF_DIMENSIONS)]
        return sequence.reshape(sequence.shape[0], -1), names
    if mode == "temporal_stats":
        components = [
            sequence.mean(axis=1),
            sequence.std(axis=1),
            sequence[:, -1] - sequence[:, 0],
            _linear_slope(sequence),
        ]
        names = [f"{stat}_A{band:02d}"
                 for stat in ("mean", "std", "delta", "slope")
                 for band in range(AEF_DIMENSIONS)]
        return np.concatenate(components, axis=1), names
    raise ValueError("fusion mode must be concat or temporal_stats")


def matrix_for_window(
    frame: pd.DataFrame,
    years: Sequence[int],
    *,
    fusion: str = "concat",
    spatial_type: str = "center",
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    years = tuple(int(year) for year in years)
    if not years or list(years) != sorted(set(years)):
        raise ValueError("years must be a non-empty, strictly increasing sequence")
    columns = [feature_columns(year, spatial_type) for year in years]
    missing = [name for group in columns for name in group if name not in frame.columns]
    if missing:
        raise ValueError(f"Missing {len(missing)} AEF columns; examples={missing[:5]}")
    sequence = np.stack(
        [frame[group].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float64)
         for group in columns],
        axis=1,
    )
    valid = np.isfinite(sequence).all(axis=(1, 2))
    valid &= ~np.isclose(np.linalg.norm(sequence, axis=2), 0.0).any(axis=1)
    fused, names = fuse_years(sequence[valid], fusion)
    return fused, valid, names


def trailing_windows(years: Sequence[int], lengths: Sequence[int]) -> list[tuple[int, ...]]:
    available = sorted(set(int(year) for year in years))
    windows: list[tuple[int, ...]] = []
    for end_index, end_year in enumerate(available):
        for length in sorted(set(int(value) for value in lengths)):
            start_year = end_year - length + 1
            window = tuple(range(start_year, end_year + 1))
            if length > 0 and all(year in available for year in window):
                windows.append(window)
    return windows
