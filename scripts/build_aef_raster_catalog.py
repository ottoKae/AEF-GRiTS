#!/usr/bin/env python3
"""Build one annual VRT per AEF export and write a raster catalog."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import rasterio
from osgeo import gdal


EXPECTED_BANDS = [f"A{i:02d}" for i in range(64)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--tile-id", required=True)
    parser.add_argument("--years", type=int, nargs="+", default=list(range(2017, 2026)))
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gdal.UseExceptions()
    with rasterio.open(args.reference) as reference:
        expected = {
            "crs": reference.crs,
            "transform": reference.transform,
            "width": reference.width,
            "height": reference.height,
        }

    rows, reports = [], []
    vrt_dir = args.root / "vrt"
    vrt_dir.mkdir(parents=True, exist_ok=True)
    for year in sorted(set(args.years)):
        sources = sorted(args.root.glob(f"{args.prefix}_{year}_64band_30m*.tif"))
        if not sources:
            raise FileNotFoundError(f"no downloaded AEF GeoTIFFs for {year} in {args.root}")
        for source in sources:
            with rasterio.open(source) as dataset:
                if dataset.count != 64:
                    raise ValueError(f"expected 64 bands in {source}, got {dataset.count}")
                descriptions = list(dataset.descriptions)
                if any(descriptions) and descriptions != EXPECTED_BANDS:
                    raise ValueError(f"unexpected AEF band descriptions in {source}")

        vrt = vrt_dir / f"{args.prefix}_{year}_64band_30m.vrt"
        options = gdal.BuildVRTOptions(resampleAlg="nearest", resolution="highest")
        dataset = gdal.BuildVRT(
            str(vrt), [str(path.resolve()) for path in sources], options=options
        )
        if dataset is None:
            raise RuntimeError(f"GDAL failed to build {vrt}")
        dataset.FlushCache()
        dataset = None

        with rasterio.open(vrt) as merged:
            actual = {
                "crs": merged.crs,
                "transform": merged.transform,
                "width": merged.width,
                "height": merged.height,
            }
            if actual != expected:
                raise ValueError(f"annual VRT grid mismatch for {year}: {actual} != {expected}")
            if merged.count != 64:
                raise ValueError(f"annual VRT must have 64 bands for {year}")
        rows.append({"year": year, "tile_id": args.tile_id, "path": str(vrt.resolve())})
        reports.append(
            {
                "year": year,
                "source_files": len(sources),
                "source_bytes": sum(path.stat().st_size for path in sources),
                "vrt": str(vrt.resolve()),
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(args.out, index=False)
    report = {
        "catalog": str(args.out.resolve()),
        "tile_id": args.tile_id,
        "years": reports,
    }
    args.out.with_suffix(".report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
