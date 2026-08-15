#!/usr/bin/env python
"""Combine explicit AEF download reports into one calibration table and JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json, write_parquet


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", nargs="+", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


def _flatten(path: Path, payload: dict) -> dict:
    telemetry = payload.get("telemetry") or {}
    resource = payload.get("resource_plan") or {}
    memory = payload.get("memory") or {}
    return {
        "report_path": str(path.resolve()),
        "workflow": resource.get("workflow") or payload.get("workflow") or "grid",
        "scheme": payload.get("scheme"),
        "grid_id": payload.get("grid_id"),
        "width": payload.get("width"),
        "height": payload.get("height"),
        "sample_count": payload.get("sample_count"),
        "blocks": payload.get("blocks"),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "resource_profile": (payload.get("resource_profile") or {}).get("name"),
        "workers": resource.get("workers"),
        "max_in_flight": resource.get("max_in_flight"),
        "effective_available_gib": (
            memory.get("effective_available_bytes", 0) / 1024**3
            if memory.get("effective_available_bytes") is not None
            else None
        ),
        "estimated_peak_gib": resource.get("estimated_peak_gib"),
        "observed_peak_gib": (
            float(payload["peak_rss_bytes"]) / 1024**3
            if payload.get("peak_rss_bytes") is not None
            else (
                float(payload["peak_rss_mb"]) / 1024
                if payload.get("peak_rss_mb") is not None
                else None
            )
        ),
        "requests_succeeded": telemetry.get("requests_succeeded"),
        "request_retries": telemetry.get("request_retries"),
        "request_timeouts": telemetry.get("request_timeouts"),
        "request_p50_seconds": telemetry.get("request_latency_seconds_p50"),
        "request_p95_seconds": telemetry.get("request_latency_seconds_p95"),
        "response_mib_estimated": telemetry.get(
            "earth_engine_response_mib_estimated"
        ),
        "token_wait_seconds": telemetry.get("request_token_wait_seconds"),
        "write_seconds": telemetry.get("zarr_or_parquet_write_seconds"),
        "delivery_seconds": telemetry.get("final_delivery_seconds"),
    }


def main() -> None:
    args = parse_args()
    rows = []
    for path in args.reports:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.append(_flatten(path, payload))
    if not rows:
        raise ValueError("At least one report is required")
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    table_path = output / "download_benchmarks.parquet"
    write_parquet(frame, table_path)
    observed = [float(value) for value in frame.observed_peak_gib.dropna()]
    estimated = [float(value) for value in frame.estimated_peak_gib.dropna()]
    summary = {
        "reports": len(frame),
        "table": str(table_path),
        "workflows": frame.workflow.value_counts(dropna=False).to_dict(),
        "median_observed_peak_gib": statistics.median(observed) if observed else None,
        "max_observed_peak_gib": max(observed) if observed else None,
        "median_estimated_peak_gib": statistics.median(estimated) if estimated else None,
        "calibration_ready": bool(
            (frame.workflow == "points").any()
            and (frame.scheme == "tessera_0p1").any()
            and (
                (frame.scheme == "mgrs")
                & (pd.to_numeric(frame.width, errors="coerce") >= 10_000)
                & (pd.to_numeric(frame.height, errors="coerce") >= 10_000)
            ).any()
        ),
        "note": (
            "Do not relax conservative limits until the recovered server has "
            "point, Tessera and complete MGRS-year observations."
        ),
    }
    write_json(summary, output / "summary.json")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
