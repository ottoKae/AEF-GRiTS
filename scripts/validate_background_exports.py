"""Validate, merge, and quality-control global-background 2025 AEF exports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MASTER = (
    ROOT / "outputs/global_background_2025/global_background_points_2025.csv"
)
DEFAULT_RAW = ROOT / "outputs/global_background_2025/raw_exports"
DEFAULT_OUTPUT = ROOT / "outputs/global_background_2025/features"
FEATURES = [f"aef2025_center_A{dimension:02d}" for dimension in range(64)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pattern", default="aef_global_background_2025_center_part*.csv")
    return parser.parse_args()


def validate_exports(
    master: pd.DataFrame,
    paths: list[Path],
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    required = {"sample_id", "lon", "lat"}
    if not required.issubset(master.columns):
        raise ValueError(f"master lacks columns={sorted(required - set(master.columns))}")
    if master.sample_id.duplicated().any():
        raise ValueError("master sample IDs are not unique")
    if not paths:
        raise FileNotFoundError("no GEE CSV exports were supplied")

    frames = []
    file_rows = {}
    for path in paths:
        frame = pd.read_csv(path)
        missing = sorted(set(["sample_id", *FEATURES]) - set(frame.columns))
        if missing:
            raise ValueError(f"{path} lacks columns={missing[:8]}")
        local = frame[["sample_id", *FEATURES]].copy()
        frames.append(local)
        file_rows[str(path)] = int(len(local))
    exported = pd.concat(frames, ignore_index=True)
    duplicate_ids = exported.loc[exported.sample_id.duplicated(False), "sample_id"].unique()
    if len(duplicate_ids):
        raise ValueError(f"duplicate exported IDs; examples={duplicate_ids[:8].tolist()}")
    unknown = sorted(set(exported.sample_id) - set(master.sample_id))
    if unknown:
        raise ValueError(f"exports contain {len(unknown)} IDs absent from master")

    combined = master.merge(exported, on="sample_id", how="left", validate="one_to_one")
    values = combined[FEATURES].apply(pd.to_numeric, errors="coerce")
    complete = np.isfinite(values.to_numpy(dtype=np.float64)).all(axis=1)
    retained = combined.loc[complete].copy()
    retained[FEATURES] = values.loc[complete]
    excluded = combined.loc[~complete, master.columns].copy()
    retained_values = retained[FEATURES].to_numpy(dtype=np.float64)
    norms = np.linalg.norm(retained_values, axis=1) if len(retained) else np.array([])
    qc = {
        "master_rows": int(len(master)),
        "input_files": len(paths),
        "input_file_rows": file_rows,
        "exported_unique_ids": int(exported.sample_id.nunique()),
        "retained_complete_rows": int(len(retained)),
        "excluded_incomplete_rows": int(len(excluded)),
        "coverage_fraction": float(len(retained) / len(master)),
        "feature_columns": len(FEATURES),
        "nonfinite_feature_values": int((~np.isfinite(values.to_numpy(dtype=np.float64))).sum()),
        "feature_min": float(np.nanmin(retained_values)) if len(retained) else None,
        "feature_max": float(np.nanmax(retained_values)) if len(retained) else None,
        "vector_norm": {
            "min": float(norms.min()) if len(norms) else None,
            "mean": float(norms.mean()) if len(norms) else None,
            "max": float(norms.max()) if len(norms) else None,
            "std": float(norms.std()) if len(norms) else None,
        },
        "complete_by_tile": retained.tile_id.value_counts().sort_index().to_dict(),
        "excluded_by_tile": excluded.tile_id.value_counts().sort_index().to_dict(),
        "pass": bool(len(retained) == len(master) and len(retained) == 40_000),
    }
    return retained, excluded, qc


def main() -> None:
    args = parse_args()
    if not args.master.exists():
        raise FileNotFoundError(args.master)
    paths = args.inputs or sorted(args.raw_dir.glob(args.pattern))
    retained, excluded, qc = validate_exports(pd.read_csv(args.master), paths)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    retained.to_parquet(args.out_dir / "global_background_aef_2025.parquet", index=False)
    retained.to_csv(args.out_dir / "global_background_aef_2025.csv", index=False)
    excluded.to_csv(args.out_dir / "global_background_aef_2025_incomplete.csv", index=False)
    (args.out_dir / "global_background_aef_2025_qc.json").write_text(
        json.dumps(qc, indent=2), encoding="utf-8",
    )
    print(json.dumps(qc, indent=2), flush=True)
    if not qc["pass"]:
        raise RuntimeError(
            "AEF background export is incomplete; inspect the QC JSON and resubmit missing IDs"
        )


if __name__ == "__main__":
    main()
