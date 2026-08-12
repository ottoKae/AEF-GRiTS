from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from scripts.visualize_aef_grid_lookup import load_intersections, render_lookup


def test_grid_lookup_quicklook_writes_png_svg_and_report(tmp_path: Path):
    rows = pd.DataFrame(
        {
            "scheme": ["mgrs", "tessera_0p1", "tessera_0p1"],
            "grid_id": ["50SMA", "grid_116.05_31.05", "grid_116.15_31.05"],
            "geometry": [
                box(115.8, 30.8, 116.8, 31.8).wkb,
                box(116.0, 31.0, 116.1, 31.1).wkb,
                box(116.1, 31.0, 116.2, 31.1).wkb,
            ],
        }
    )
    intersections_path = tmp_path / "grid_intersections.parquet"
    rows.to_parquet(intersections_path, index=False)
    intersections = load_intersections(intersections_path)
    aoi = gpd.GeoDataFrame(
        {"name": ["test"]}, geometry=[box(116.02, 31.01, 116.18, 31.09)], crs=4326
    )
    output = tmp_path / "coverage.png"

    report = render_lookup(intersections, output, aoi=aoi, title="Test AOI")

    assert output.is_file()
    assert output.with_suffix(".svg").is_file()
    assert output.with_suffix(".report.json").is_file()
    assert report["mgrs_grids"] == 1
    assert report["tessera_0p1_grids"] == 2
