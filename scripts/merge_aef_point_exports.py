#!/usr/bin/env python
"""Validate and merge chunked 2017-2025 GEE AEF point exports with the sample master."""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aef_grits.features import discover_years, feature_columns


def merge_exports(master: pd.DataFrame, export_paths: list[Path]) -> tuple[pd.DataFrame, dict]:
    if "sample_id" not in master or master.sample_id.duplicated().any():
        raise ValueError("master must contain unique sample_id")
    by_year: dict[int, list[pd.DataFrame]] = {}
    file_rows = []
    for path in export_paths:
        frame = pd.read_csv(path, low_memory=False)
        if "sample_id" not in frame:
            raise ValueError(f"AEF export lacks sample_id: {path}")
        years = discover_years(frame.columns)
        if not years:
            raise ValueError(f"export chunk contains no complete 64-D year: {path}")
        for year in years:
            columns = feature_columns(year)
            local = frame[["sample_id", *columns]].copy()
            by_year.setdefault(year, []).append(local)
        file_rows.append({"path": str(path.resolve()), "years": years, "rows": len(frame)})
    if not by_year:
        raise ValueError("no AEF exports supplied")
    merged_features = None
    year_report = {}
    master_ids = set(master.sample_id.astype(str))
    for year in sorted(by_year):
        frame = pd.concat(by_year[year], ignore_index=True)
        if frame.sample_id.duplicated().any():
            examples = frame.loc[frame.sample_id.duplicated(), "sample_id"].head().tolist()
            raise ValueError(f"duplicate sample_id across {year} chunks: {examples}")
        frame["sample_id"] = frame.sample_id.astype(str)
        extra = set(frame.sample_id) - master_ids
        if extra:
            raise ValueError(f"{year} export has IDs absent from master: {sorted(extra)[:5]}")
        values = frame[feature_columns(year)].apply(pd.to_numeric, errors="coerce")
        frame[feature_columns(year)] = values
        complete = np.isfinite(values.to_numpy(dtype=float)).all(axis=1)
        year_report[str(year)] = {
            "rows": len(frame), "complete_rows": int(complete.sum()),
            "missing_master_ids": int(len(master_ids - set(frame.sample_id))),
            "nonfinite_values": int((~np.isfinite(values.to_numpy(dtype=float))).sum()),
        }
        merged_features = frame if merged_features is None else merged_features.merge(
            frame, on="sample_id", how="outer", validate="one_to_one"
        )
    master = master.copy(); master["sample_id"] = master.sample_id.astype(str)
    merged = master.merge(merged_features, on="sample_id", how="left", validate="one_to_one")
    report = {
        "master_rows": len(master), "output_rows": len(merged),
        "years": sorted(by_year), "files": file_rows, "year_qc": year_report,
        "complete_all_years_rows": int(merged[
            [column for year in sorted(by_year) for column in feature_columns(year)]
        ].notna().all(axis=1).sum()),
    }
    return merged, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--exports", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    master = pd.read_csv(args.master, low_memory=False)
    merged, report = merge_exports(master, args.exports)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.suffix.lower() == ".parquet":
        merged.to_parquet(args.out, index=False)
    else:
        merged.to_csv(args.out, index=False)
    Path(str(args.out) + ".report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
