"""Isolated structural validation for completed point and grid deliveries."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from aef_grits.atomic import write_json


def validate_delivery(payload: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(payload["output_dir"]).resolve()
    control_dir = Path(payload["control_dir"]).resolve()
    params = payload["params"]
    errors: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {
        "workflow": params["workflow"],
        "output_dir": str(output_dir),
    }
    if params["workflow"] == "points":
        report_path = control_dir / "report.json"
        catalog_path = control_dir / "catalog.parquet"
        if not report_path.exists():
            errors.append("Missing point report.json")
        if not catalog_path.exists():
            errors.append("Missing point catalog.parquet")
        if not errors:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            catalog = pd.read_parquet(catalog_path)
            expected = int(params.get("sample_count", 0))
            complete = int(report.get("complete_rows", -1))
            incomplete = int(report.get("incomplete_rows", -1))
            if int(report.get("sample_count", -1)) != expected:
                errors.append("Point report sample_count differs from the accepted plan")
            if complete + incomplete != expected:
                errors.append("Point complete/incomplete counts do not sum to sample_count")
            if incomplete:
                warnings.append(f"{incomplete} point rows have incomplete AEF features")
            shard_paths = [Path(value) for value in catalog.get("path", [])]
            missing_shards = [str(path) for path in shard_paths if not path.exists()]
            if missing_shards:
                errors.append(f"Missing {len(missing_shards)} point shards")
            summary.update(
                {
                    "sample_count": expected,
                    "complete_rows": complete,
                    "incomplete_rows": incomplete,
                    "shards": len(catalog),
                    "feature_count": len(params["years"]) * 64,
                    "catalog": str(catalog_path),
                    "report": str(report_path),
                }
            )
    else:
        import xarray as xr

        report_path = control_dir / "run_summary.json"
        catalog_path = control_dir / "catalog.parquet"
        if not report_path.exists():
            errors.append("Missing grid run_summary.json")
        if not catalog_path.exists():
            errors.append("Missing grid catalog.parquet")
        grids = []
        if not errors:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            catalog = pd.read_parquet(catalog_path)
            expected_ids = set(params.get("grid_ids", []))
            actual_ids = set(catalog.grid_id.astype(str)) if "grid_id" in catalog else set()
            if expected_ids != actual_ids:
                errors.append(
                    f"Catalog grid IDs differ from plan: expected={sorted(expected_ids)}, "
                    f"actual={sorted(actual_ids)}"
                )
            if "status" not in catalog or not catalog.status.astype(str).eq("complete").all():
                errors.append("Not every grid catalog row is complete")
            if int(report.get("completed", -1)) != len(expected_ids) or report.get("failed"):
                errors.append("Grid run summary is incomplete")
            for row in catalog.itertuples(index=False):
                zarr_path = Path(str(row.zarr_path)).resolve()
                if output_dir not in zarr_path.parents:
                    errors.append(f"Catalog path escapes task output directory: {zarr_path}")
                    continue
                if not zarr_path.exists():
                    errors.append(f"Missing Zarr store: {zarr_path}")
                    continue
                try:
                    dataset = xr.open_zarr(zarr_path, consolidated=True)
                    array = dataset["embeddings"]
                    if (
                        array.ndim != 4
                        or array.shape[0] != len(params["years"])
                        or array.shape[1] != 64
                    ):
                        errors.append(f"Unexpected embedding shape for {row.grid_id}: {array.shape}")
                    grids.append(
                        {
                            "grid_id": str(row.grid_id),
                            "zarr_path": str(zarr_path),
                            "shape": list(array.shape),
                            "chunks": [list(chunk) for chunk in array.chunks],
                            "crs": str(row.crs),
                            "resolution_m": float(row.resolution_m),
                            "compression": {
                                "codec": str(row.compression_codec),
                                "level": int(row.compression_level),
                                "shuffle": str(row.compression_shuffle),
                            },
                        }
                    )
                    dataset.close()
                except Exception as exc:
                    errors.append(f"Cannot open {zarr_path}: {exc}")
            summary.update(
                {
                    "grid_count": len(expected_ids),
                    "grids": grids,
                    "catalog": str(catalog_path),
                    "report": str(report_path),
                }
            )
    return {
        **summary,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "validated_at": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--final", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if Path(payload["output_dir"]).absolute() != args.final.absolute():
        raise ValueError("Validation final path differs from the signed worker argument")
    write_json(validate_delivery(payload), args.output)


if __name__ == "__main__":
    main()
