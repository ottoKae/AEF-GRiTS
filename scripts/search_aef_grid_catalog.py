#!/usr/bin/env python
"""Search a bilingual AEF grid catalog by region name, code or grid ID."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--scheme", choices=("mgrs", "tessera_0p1"))
    parser.add_argument("--ids-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    frame = pd.read_parquet(args.catalog)
    required = {"scheme", "grid_id", "search_text"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Catalog missing columns: {sorted(missing)}")
    selected = frame.loc[
        frame.search_text.fillna("").astype(str).str.contains(
            args.query.casefold(), regex=False
        )
    ].copy()
    if args.scheme:
        selected = selected.loc[selected.scheme == args.scheme]
    selected = selected.sort_values(["scheme", "grid_id"])
    if args.ids_only:
        for value in selected.grid_id.astype(str):
            print(value)
        return
    columns = [
        column
        for column in (
            "scheme",
            "grid_id",
            "utm_epsg",
            "region_ids",
            "region_names_cn",
            "region_names_en",
        )
        if column in selected
    ]
    print(
        json.dumps(
            selected[columns].to_dict(orient="records"),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
