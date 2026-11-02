"""Small, real raster tests for the experimental official COG reader."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.windows import Window
from rasterio.warp import transform_bounds

from aef_grits import public_aef
from aef_grits.grids import GridSpec


@pytest.fixture
def indexed_rasters(tmp_path):
    affine = Affine(10, 0, 500000, 0, -10, 1000)
    bounds = (500000, 920, 500080, 1000)
    geographic = transform_bounds("EPSG:32631", "EPSG:4326", *bounds)
    records = []
    for year, code in ((2024, 32), (2025, -64)):
        path = tmp_path / f"aef_{year}.tif"
        values = np.full((64, 8, 8), code, dtype=np.int8)
        values[:, 0, 0] = -128
        values[:, 3, 4] = 0
        with rasterio.open(
            path, "w", driver="GTiff", width=8, height=8, count=64,
            dtype="int8", crs="EPSG:32631", transform=affine, nodata=-128,
        ) as target:
            target.write(values)
            target.descriptions = public_aef.BANDS
        records.append(dict(
            path=str(path), year=year, crs="EPSG:32631",
            **dict(zip(("utm_west", "utm_south", "utm_east", "utm_north"), bounds)),
            **dict(zip(("wgs84_west", "wgs84_south", "wgs84_east", "wgs84_north"), geographic)),
        ))
    return public_aef.PublicAEFIndex(pd.DataFrame(records)), affine, bounds


def test_projected_points_year_order_nodata_and_zero(indexed_rasters):
    index, affine, _ = indexed_rasters
    coords = [affine * (1.5, 1.5), affine * (0.5, 0.5), affine * (4.5, 3.5)]
    actual = index.sample_points(coords, [2025, 2024], crs="EPSG:32631")
    assert actual.shape == (3, 2, 64)
    np.testing.assert_allclose(actual[0, 0], -(64 / 127.5) ** 2, atol=1e-7)
    np.testing.assert_allclose(actual[0, 1], (32 / 127.5) ** 2, atol=1e-7)
    assert np.isnan(actual[1]).all()
    assert np.equal(actual[2], 0).all()


def test_grid_block_keeps_transform_and_year(indexed_rasters):
    index, affine, bounds = indexed_rasters
    spec = GridSpec("reference", "tiny", "EPSG:32631", affine, 8, 8, bounds)
    block = index.read_grid_block(spec, 2025, Window(0, 0, 6, 5))
    assert block.shape == (64, 5, 6)
    assert block.dtype == np.float32
    assert np.isnan(block[:, 0, 0]).all()
    assert np.equal(block[:, 3, 4], 0).all()
    np.testing.assert_allclose(block[:, 1, 1], -(64 / 127.5) ** 2, atol=1e-7)
    assert np.isnan(index.read_grid_block(spec, 2023, Window(0, 0, 1, 1))).all()
    with pytest.raises(ValueError, match="inside"):
        index.read_grid_block(spec, 2025, Window(7, 7, 2, 2))


def test_wgs84_points_and_reject_other_utm_target(indexed_rasters):
    index, affine, _ = indexed_rasters
    x, y = affine * (2.5, 2.5)
    lon, lat = Transformer.from_crs(32631, 4326, always_xy=True).transform(x, y)
    points = index.sample_points([(lon, lat)], [2024])
    np.testing.assert_allclose(points, (32 / 127.5) ** 2, atol=1e-7)
    target_x, target_y = Transformer.from_crs(32631, 32632, always_xy=True).transform(x, y)
    target_transform = Affine(10, 0, target_x - 5, 0, -10, target_y + 5)
    spec = GridSpec("reference", "other-zone", "EPSG:32632", target_transform, 1, 1,
                    (target_x - 5, target_y - 5, target_x + 5, target_y + 5))
    with pytest.raises(ValueError, match="No native AEF source"):
        index.read_grid_block(spec, 2024, Window(0, 0, 1, 1))


def test_reject_fractional_pixel_shift_and_read_parquet_index(indexed_rasters, tmp_path):
    index, _, _ = indexed_rasters
    path = tmp_path / "index.parquet"
    index.frame.assign(geom=[b"unused", b"unused"]).to_parquet(path)
    loaded = public_aef.PublicAEFIndex.from_parquet(path)
    affine = Affine(10, 0, 500003, 0, -10, 997)
    spec = GridSpec("reference", "shifted", "EPSG:32631", affine, 7, 7,
                    (500003, 927, 500073, 997))
    with pytest.raises(ValueError, match="fractional-pixel"):
        loaded.read_grid_block(spec, 2024, Window(0, 0, 5, 4))


def test_integer_shift_reads_exact_pixels_and_preserves_outside_mask(indexed_rasters):
    index, affine, _ = indexed_rasters
    transform = affine * Affine.translation(-2, -1)
    spec = GridSpec("reference", "integer-shift", "EPSG:32631", transform, 8, 7,
                    (499980, 940, 500060, 1010))
    actual = index.read_grid_block(spec, 2024, Window(1, 0, 7, 6))
    assert np.isnan(actual[:, 0]).all()
    assert np.isnan(actual[:, :, 0]).all()
    assert np.isnan(actual[:, 1, 1]).all()
    assert np.equal(actual[:, 4, 5], 0).all()
    with rasterio.open(index.frame.iloc[0].path) as source:
        expected = public_aef._decode(source.read(window=Window(0, 0, 6, 5), masked=True))
    np.testing.assert_array_equal(actual[:, 1:, 1:], expected)


def test_mixed_utm_candidates_do_not_fill_missing_native_pixels(indexed_rasters, tmp_path):
    index, affine, bounds = indexed_rasters
    x, y = Transformer.from_crs(32631, 32632, always_xy=True).transform(500000, 1000)
    foreign_transform = Affine(10, 0, x, 0, -10, y)
    path = tmp_path / "000_foreign_zone.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=8, height=8, count=64,
        dtype="int8", crs="EPSG:32632", transform=foreign_transform, nodata=-128,
    ) as target:
        target.write(np.full((64, 8, 8), 127, dtype=np.int8))
        target.descriptions = public_aef.BANDS
    foreign = index.frame.iloc[[0]].assign(
        path=str(path), crs="EPSG:32632", utm_west=x, utm_east=x + 80,
        utm_south=y - 80, utm_north=y,
    )
    combined = public_aef.PublicAEFIndex(pd.concat([index.frame, foreign], ignore_index=True))
    spec = GridSpec("reference", "tiny", "EPSG:32631", affine, 8, 8, bounds)
    actual = combined.read_grid_block(spec, 2024, Window(0, 0, 8, 8))
    expected = index.read_grid_block(spec, 2024, Window(0, 0, 8, 8))
    np.testing.assert_array_equal(actual, expected)
    assert np.isnan(actual[:, 0, 0]).all()
    assert public_aef.PROVENANCE["resampling"] == "none"


def test_reject_unknown_index_and_raster_contract(indexed_rasters):
    index, affine, bounds = indexed_rasters
    with pytest.raises(ValueError, match="missing columns"):
        public_aef.PublicAEFIndex(pd.DataFrame({"url": ["unknown"]}))
    with pytest.raises(ValueError, match="official GCS"):
        public_aef.PublicAEFIndex(index.frame.assign(path="http://untrusted.invalid/a.tif"))
    with rasterio.open(index.frame.iloc[0].path, "r+") as target:
        target.set_band_description(1, "wrong")
    spec = GridSpec("reference", "tiny", "EPSG:32631", affine, 8, 8, bounds)
    with pytest.raises(ValueError, match="A00"):
        index.read_grid_block(spec, 2024, Window(0, 0, 1, 1))


def test_bounded_http_probe_does_not_claim_extraction_ready(monkeypatch):
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(self.headers.get("Range"))
            payload = (
                b"WKT,crs,path,year,utm_zone,".ljust(256, b" ")
                if self.path == "/index.csv" else b"\x01\x00\x00\x00PAR1"
            )
            self.send_response(206)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Range", f"bytes 0-{len(payload)-1}/1000000")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(public_aef, "INDEX_CSV_URL", root + "/index.csv")
        monkeypatch.setattr(public_aef, "INDEX_PARQUET_URL", root + "/index.parquet")
        result = public_aef.probe_public_source()
        assert result["reachable"] is True
        assert result["extraction_ready"] is False
        assert result["index_bytes"] == 1000000
        assert requests_seen == ["bytes=0-255", "bytes=-8"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
