#!/usr/bin/env python
"""Aggregate multi-point annual AEF samples into polygon-equal prototypes.

Input is the wide table produced by ``merge_aef_point_exports.py``. Each AEF
pixel is first L2-normalized; the polygon prototype is the normalized weighted
mean direction.  This preserves AlphaEarth's cosine geometry and prevents
large polygons from dominating later clustering.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aef_grits.features import discover_years, feature_columns, l2_normalize  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pixel-features", type=Path, required=True)
    parser.add_argument(
        "--out-dir", type=Path,
        default=PROJECT_ROOT / "outputs/polygon_features",
    )
    parser.add_argument("--years", nargs="+", type=int)
    parser.add_argument("--minimum-valid-fraction", type=float, default=1.0)
    parser.add_argument("--output-prefix", default="plantation_polygon_aef")
    parser.add_argument(
        "--label-status", default="inventory_presence_not_annual_state"
    )
    return parser.parse_args()


def read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path, low_memory=False)


def _anchor_metadata(frame: pd.DataFrame) -> pd.DataFrame:
    local = frame.copy()
    if "is_anchor" in local:
        anchor_flag = local["is_anchor"].astype(str).str.lower().isin({"true", "1", "yes"})
        local = pd.concat([local.loc[anchor_flag], local.loc[~anchor_flag]])
    elif "point_rank" in local:
        local = local.sort_values(["polygon_id", "point_rank", "sample_id"])
    else:
        local = local.sort_values(["polygon_id", "sample_id"])
    return local.drop_duplicates("polygon_id", keep="first").set_index("polygon_id")


def aggregate_polygon_features(
    pixels: pd.DataFrame,
    years: list[int],
    *,
    minimum_valid_fraction: float = 1.0,
    label_status: str = "generic_balsa_presence_not_annual_maturity",
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    required = {"sample_id", "polygon_id"}
    missing = required - set(pixels.columns)
    if missing:
        raise ValueError(f"pixel feature table missing columns: {sorted(missing)}")
    if pixels["sample_id"].duplicated().any():
        raise ValueError("pixel feature sample_id must be unique")
    if "label" in pixels:
        pixels = pixels.loc[pd.to_numeric(pixels["label"], errors="coerce").eq(1)].copy()
    if pixels.empty:
        raise ValueError("no labelled polygon pixel rows remain")
    if not 0 < minimum_valid_fraction <= 1:
        raise ValueError("minimum valid fraction must be in (0, 1]")

    pixels = pixels.reset_index(drop=True)
    anchors = _anchor_metadata(pixels)
    polygon_ids = pd.Index(sorted(pixels["polygon_id"].astype(str).unique()), name="polygon_id")
    grouped_indices = {
        str(polygon_id): np.asarray(indices, dtype=int)
        for polygon_id, indices in pixels.groupby(pixels["polygon_id"].astype(str), sort=False).groups.items()
    }
    expected_n = pd.Series({polygon_id: len(indices) for polygon_id, indices in grouped_indices.items()})
    prototype_frames: list[pd.DataFrame] = []
    diagnostics: list[dict[str, object]] = []
    year_report: dict[str, object] = {}

    for year in years:
        columns = feature_columns(year)
        absent = [column for column in columns if column not in pixels]
        if absent:
            raise ValueError(f"{year}: missing AEF columns, examples={absent[:5]}")
        values = pixels[columns].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float64)
        valid = np.isfinite(values).all(axis=1)
        valid &= ~np.isclose(np.linalg.norm(np.nan_to_num(values), axis=1), 0.0)
        output = np.full((len(polygon_ids), 64), np.nan, dtype=np.float64)
        complete_polygons = 0
        usable_polygons = 0
        for polygon_position, polygon_id in enumerate(polygon_ids):
            indices = grouped_indices[str(polygon_id)]
            local_indices = indices[valid[indices]]
            n_expected = len(indices)
            n_valid = len(local_indices)
            valid_fraction = n_valid / n_expected
            diagnostic: dict[str, object] = {
                "polygon_id": polygon_id,
                "year": year,
                "n_expected_points": n_expected,
                "n_valid_points": n_valid,
                "valid_fraction": valid_fraction,
                "prototype_valid": False,
                "resultant_length": np.nan,
                "mean_cosine_to_prototype": np.nan,
                "p10_cosine_to_prototype": np.nan,
                "mean_angular_deviation_degrees": np.nan,
            }
            if n_valid and valid_fraction >= minimum_valid_fraction:
                unit = l2_normalize(values[local_indices])
                if "polygon_equal_weight" in pixels:
                    weights = pd.to_numeric(
                        pixels.loc[local_indices, "polygon_equal_weight"], errors="coerce"
                    ).to_numpy(dtype=float)
                    if not np.isfinite(weights).all() or weights.sum() <= 0:
                        weights = np.ones(n_valid, dtype=float)
                else:
                    weights = np.ones(n_valid, dtype=float)
                weights = weights / weights.sum()
                mean_direction = np.sum(unit * weights[:, None], axis=0)
                resultant = float(np.linalg.norm(mean_direction))
                if resultant > np.finfo(float).eps:
                    prototype = mean_direction / resultant
                    output[polygon_position] = prototype
                    cosine = np.clip(unit @ prototype, -1.0, 1.0)
                    diagnostic.update(
                        {
                            "prototype_valid": True,
                            "resultant_length": resultant,
                            "mean_cosine_to_prototype": float(np.average(cosine, weights=weights)),
                            "p10_cosine_to_prototype": float(np.quantile(cosine, 0.10)),
                            "mean_angular_deviation_degrees": float(
                                np.average(np.degrees(np.arccos(cosine)), weights=weights)
                            ),
                        }
                    )
                    usable_polygons += 1
                    complete_polygons += int(n_valid == n_expected)
            diagnostics.append(diagnostic)
        prototype_frames.append(pd.DataFrame(output, index=polygon_ids, columns=columns))
        year_report[str(year)] = {
            "valid_pixel_rows": int(valid.sum()),
            "invalid_pixel_rows": int((~valid).sum()),
            "usable_polygon_prototypes": usable_polygons,
            "complete_polygon_prototypes": complete_polygons,
        }

    prototypes = pd.concat(prototype_frames, axis=1)
    anchor_columns = [
        column for column in (
            "source_index", "tile_id", "polygon_tile_id", "s1_tile_id",
            "polygon_s1_tile_id", "inside_11_tiles", "inside_11_s1_grids",
            "polygon_area_ha", "model_center_x", "model_center_y", "lon", "lat",
            "source_objectid", "source_csp", "source_sce_ha", "province", "canton", "parish",
            "source_nco", "species_nco", "species_csp",
        ) if column in anchors
    ]
    metadata = anchors.reindex(polygon_ids)[anchor_columns].copy()
    if "polygon_tile_id" in metadata:
        metadata["tile_id"] = metadata["polygon_tile_id"].fillna(metadata.get("tile_id"))
    metadata.insert(0, "sample_id", polygon_ids.astype(str))
    metadata.insert(1, "polygon_id", polygon_ids.astype(str))
    metadata["label"] = 1
    metadata["split"] = "full_inventory"
    metadata["fold"] = -1
    metadata["phase2_label_status"] = label_status
    metadata["feature_semantics"] = "polygon_equal_spherical_mean_of_interior_AEF_pixels"
    metadata["n_points_polygon"] = expected_n.reindex(polygon_ids).to_numpy(dtype=int)
    result = metadata.reset_index(drop=True).join(prototypes.reset_index(drop=True))
    diagnostic_frame = pd.DataFrame(diagnostics)
    valid_by_year = diagnostic_frame.pivot(index="polygon_id", columns="year", values="prototype_valid")
    complete_mask = valid_by_year.reindex(index=polygon_ids, columns=years).fillna(False).all(axis=1)
    result["complete_all_requested_years"] = complete_mask.to_numpy(dtype=bool)
    report = {
        "pixel_rows": int(len(pixels)),
        "polygons": int(len(polygon_ids)),
        "years": years,
        "minimum_valid_fraction": minimum_valid_fraction,
        "complete_all_years_polygons": int(complete_mask.sum()),
        "incomplete_polygons": int((~complete_mask).sum()),
        "year_qc": year_report,
        "aggregation": "L2-normalize pixels -> weighted mean direction -> L2-normalize prototype",
        "polygon_weight_for_clustering": "one prototype per polygon",
    }
    return result, diagnostic_frame, report


def main() -> None:
    args = parse_args()
    pixels = read_table(args.pixel_features)
    years = args.years or discover_years(pixels.columns)
    if not years:
        raise ValueError("no complete annual 64-D AEF groups discovered")
    prototypes, diagnostics, report = aggregate_polygon_features(
        pixels, sorted(set(args.years or years)),
        minimum_valid_fraction=args.minimum_valid_fraction,
        label_status=args.label_status,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.output_prefix
    prototypes.to_parquet(args.out_dir / f"{prefix}_prototypes_all.parquet", index=False)
    prototypes.loc[prototypes["complete_all_requested_years"]].to_parquet(
        args.out_dir / f"{prefix}_prototypes_complete.parquet", index=False
    )
    diagnostics.to_parquet(args.out_dir / f"{prefix}_diagnostics.parquet", index=False)
    (args.out_dir / f"{prefix}_aggregation_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({**report, "out_dir": str(args.out_dir)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
