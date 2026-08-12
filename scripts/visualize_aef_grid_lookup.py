#!/usr/bin/env python
"""Create quick-look maps from an AOI-to-grid lookup result."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
from shapely import from_wkb


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.atomic import write_json  # noqa: E402
from aef_grits.grid_lookup import read_aoi  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--intersections",
        type=Path,
        required=True,
        help="grid_intersections.parquet produced by resolve_aef_grid_ids.py",
    )
    parser.add_argument("--aoi", type=Path, help="Optional source AOI vector")
    parser.add_argument("--layer", help="GeoPackage AOI layer")
    parser.add_argument("--out", type=Path, required=True, help="Output PNG path")
    parser.add_argument("--title", default="AOI-to-grid lookup validation")
    parser.add_argument("--no-svg", action="store_true")
    parser.add_argument("--dpi", type=int, default=220)
    return parser.parse_args()


def load_intersections(path: str | Path) -> gpd.GeoDataFrame:
    """Load the lookup table's WGS84 WKB geometry with strict schema checks."""
    frame = pd.read_parquet(path)
    required = {"scheme", "grid_id", "geometry"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Intersection table missing columns: {sorted(missing)}")
    if frame.empty:
        raise ValueError("Intersection table is empty")
    geometry = from_wkb(frame.geometry.to_numpy())
    output = gpd.GeoDataFrame(
        frame.drop(columns="geometry"), geometry=geometry, crs="EPSG:4326"
    )
    if output.geometry.isna().any() or output.geometry.is_empty.any():
        raise ValueError("Intersection table contains empty geometry")
    return output


def _draw_aoi(axis, aoi: gpd.GeoDataFrame | None) -> None:
    if aoi is not None:
        aoi.boundary.plot(ax=axis, color="#CC3311", linewidth=2.0, zorder=5)


def _set_extent(axis, frame: gpd.GeoDataFrame) -> list[float]:
    west, south, east, north = map(float, frame.total_bounds)
    padding_x = max((east - west) * 0.04, 0.05)
    padding_y = max((north - south) * 0.04, 0.05)
    axis.set_xlim(west - padding_x, east + padding_x)
    axis.set_ylim(south - padding_y, north + padding_y)
    return [west, south, east, north]


def render_lookup(
    intersections: gpd.GeoDataFrame,
    output: str | Path,
    *,
    aoi: gpd.GeoDataFrame | None = None,
    title: str = "AOI-to-grid lookup validation",
    dpi: int = 220,
    write_svg: bool = True,
) -> dict:
    """Render side-by-side MGRS and Tessera coverage and return summary data."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    mgrs = intersections.loc[intersections.scheme.eq("mgrs")].drop_duplicates(
        ["grid_id"]
    )
    tessera = intersections.loc[
        intersections.scheme.eq("tessera_0p1")
    ].drop_duplicates(["grid_id"])
    if mgrs.empty and tessera.empty:
        raise ValueError("No mgrs or tessera_0p1 rows were found")

    plt.rcParams.update(
        {"font.size": 11, "axes.titlesize": 14, "axes.labelsize": 11}
    )
    figure, axes = plt.subplots(1, 2, figsize=(15, 7), constrained_layout=True)

    if not mgrs.empty:
        mgrs.boundary.plot(ax=axes[0], color="#4477AA", linewidth=0.75)
        for row in mgrs.itertuples():
            point = row.geometry.representative_point()
            axes[0].text(
                point.x,
                point.y,
                str(row.grid_id),
                ha="center",
                va="center",
                fontsize=6.5,
                bbox={
                    "facecolor": "white",
                    "edgecolor": "none",
                    "alpha": 0.65,
                    "pad": 0.4,
                },
            )
    _draw_aoi(axes[0], aoi)
    axes[0].set_title(f"MGRS coverage ({len(mgrs):,} grids)")
    mgrs_bounds = _set_extent(axes[0], mgrs if not mgrs.empty else intersections)

    if not tessera.empty:
        tessera.plot(
            ax=axes[1],
            facecolor="#DCEAF7",
            edgecolor="#4477AA",
            linewidth=0.12,
            alpha=0.75,
        )
    _draw_aoi(axes[1], aoi)
    axes[1].set_title(f"Tessera 0.1-degree coverage ({len(tessera):,} grids)")
    tessera_bounds = _set_extent(
        axes[1], tessera if not tessera.empty else intersections
    )

    for axis in axes:
        axis.set_xlabel("Longitude")
        axis.set_ylabel("Latitude")
        axis.set_aspect("equal", adjustable="box")
        axis.grid(alpha=0.2)
    figure.suptitle(title, fontsize=17)
    figure.savefig(output, dpi=dpi, bbox_inches="tight")
    svg_path = output.with_suffix(".svg")
    if write_svg:
        figure.savefig(svg_path, bbox_inches="tight")
    plt.close(figure)

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "intersections": int(len(intersections)),
        "mgrs_grids": int(len(mgrs)),
        "tessera_0p1_grids": int(len(tessera)),
        "aoi_bounds_wgs84": (
            list(map(float, aoi.total_bounds)) if aoi is not None else None
        ),
        "mgrs_bounds_wgs84": mgrs_bounds,
        "tessera_0p1_bounds_wgs84": tessera_bounds,
        "png": str(output),
        "svg": str(svg_path) if write_svg else None,
    }
    write_json(report, output.with_suffix(".report.json"))
    return report


def main() -> None:
    args = parse_args()
    intersections = load_intersections(args.intersections)
    aoi = read_aoi(args.aoi, layer=args.layer) if args.aoi else None
    report = render_lookup(
        intersections,
        args.out,
        aoi=aoi,
        title=args.title,
        dpi=args.dpi,
        write_svg=not args.no_svg,
    )
    print(
        f"Rendered MGRS={report['mgrs_grids']}, "
        f"Tessera={report['tessera_0p1_grids']}: {report['png']}"
    )


if __name__ == "__main__":
    main()
