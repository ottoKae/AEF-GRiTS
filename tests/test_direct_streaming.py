import json

import numpy as np
from affine import Affine
import pandas as pd
import pytest
import zarr

from aef_grits.grids import (
    GridSpec,
    MGRSGridProvider,
    Tessera01GridProvider,
    tessera_tile_from_world,
)
from aef_grits.store import AEFZarr
from scripts.stream_aef_grid_ee import (
    COMPRESSION_CNAME,
    COMPRESSION_LEVEL,
    COMPRESSION_PROTOCOL_VERSION,
    COMPRESSION_SHUFFLE,
    COMPRESSION_TYPESIZE,
    DEFAULT_INNER_CHUNK,
    DEFAULT_SHARD_SIZE,
    _create_store,
    _pixel_grid,
)


def test_production_zarr_layout_defaults(tmp_path):
    assert DEFAULT_INNER_CHUNK == 64
    assert DEFAULT_SHARD_SIZE == 512
    group = _create_store(
        tmp_path / "production-defaults.zarr",
        _grid(),
        [2025],
        "production-defaults",
        DEFAULT_INNER_CHUNK,
        DEFAULT_SHARD_SIZE,
    )
    array = group["embeddings"]
    assert array.chunks == (1, 64, 64, 64)
    assert array.shards == (1, 64, 512, 512)
    assert np.dtype(array.dtype) == np.dtype(np.float32)


def _grid():
    return {
        "crs": "EPSG:32717",
        "transform": Affine(10.0, 0.0, 500_000.0, 0.0, -10.0, 10_000_000.0),
        "width": 16,
        "height": 16,
        "bounds": (500_000.0, 9_999_840.0, 500_160.0, 10_000_000.0),
        "tile_id": "TEST",
        "grid_id": "TEST",
        "scheme": "reference",
    }


def test_compute_pixels_grid_is_window_aligned():
    block = {"row": 3, "column": 4, "width": 5, "height": 6}
    request_grid = _pixel_grid(_grid(), block)
    affine = request_grid["affineTransform"]
    assert request_grid["dimensions"] == {"width": 5, "height": 6}
    assert affine["translateX"] == 500_040.0
    assert affine["translateY"] == 9_999_970.0
    assert affine["scaleX"] == 10.0
    assert affine["scaleY"] == -10.0


def test_local_store_samples_touched_chunks(tmp_path):
    path = tmp_path / "test.zarr"
    group = _create_store(path, _grid(), [2024, 2025], "test-signature", 4, 8)
    values = np.arange(64, dtype=np.float32)
    group["embeddings"][1, :, 6, 7] = values
    zarr.consolidate_metadata(str(path))

    store = AEFZarr(path)
    x = 500_000.0 + (7 + 0.5) * 10.0
    y = 10_000_000.0 - (6 + 0.5) * 10.0
    actual = store.sample_at(x, y, 2025, crs="EPSG:32717")
    np.testing.assert_array_equal(actual, values)


def test_local_store_marks_outside_points_missing(tmp_path):
    path = tmp_path / "test.zarr"
    _create_store(path, _grid(), [2025], "test-signature", 4, 8)
    zarr.consolidate_metadata(str(path))
    store = AEFZarr(path)
    values = store.sample_points([(0.0, 0.0)], years=[2025], crs="EPSG:32717")
    assert values.shape == (1, 1, 64)
    assert np.isnan(values).all()


def test_tessera_grid_uses_centres_and_ten_metre_utm():
    assert tessera_tile_from_world(0.17, 52.23) == (0.15, 52.25)
    assert tessera_tile_from_world(-0.12, -0.03) == (-0.15, -0.05)
    grid = Tessera01GridProvider().get(-79.93, -1.04)
    assert grid.grid_id == "grid_-79.95_-1.05"
    assert grid.crs == "EPSG:32717"
    assert grid.transform.a == 10.0
    assert grid.transform.e == -10.0
    assert grid.width > 1_000
    assert grid.height > 1_000


def test_grid_contract_rejects_non_ten_metre_resolution():
    with pytest.raises(ValueError, match="resolution must be 10 m"):
        GridSpec(
            scheme="reference",
            grid_id="bad_30m",
            crs="EPSG:32717",
            transform=Affine(30, 0, 0, 0, -30, 300),
            width=10,
            height=10,
            bounds=(0, 0, 300, 300),
        )


def test_tessera_bbox_enumerates_each_cell_once():
    grids = Tessera01GridProvider().from_bbox((-80.0, -1.1, -79.8, -0.9))
    assert len(grids) == 4
    assert len({grid.grid_id for grid in grids}) == 4


def test_mgrs_provider_uses_packaged_utm_geometry(tmp_path):
    path = tmp_path / "mgrs.parquet"
    pd.DataFrame(
        {
            "mgrs_tile_id": ["17MPU"],
            "utm_epsg": [32717],
            "utm_wkt": [
                "MULTIPOLYGON(((600000 9900040,600000 9790240,"
                "709800 9790240,709800 9900040,600000 9900040)))"
            ],
        }
    ).to_parquet(path, index=False)
    grid = MGRSGridProvider(path).get("17MPU")
    assert grid.scheme == "mgrs"
    assert grid.grid_id == "17MPU"
    assert grid.crs == "EPSG:32717"
    assert grid.width == 10_980
    assert grid.height == 10_980
    assert grid.transform == Affine(10, 0, 600_000, 0, -10, 9_900_040)


def test_local_store_reads_window_and_bbox(tmp_path):
    path = tmp_path / "window.zarr"
    group = _create_store(path, _grid(), [2025], "test-signature", 4, 8)
    group["embeddings"][0, :, 3:5, 4:7] = 2.0
    zarr.consolidate_metadata(str(path))
    store = AEFZarr(path)
    window = store.read_window(3, 5, 4, 7, years=[2025])
    assert window.shape == (1, 64, 2, 3)
    assert np.all(window == 2.0)
    xmin = 500_000.0 + 4 * 10.0
    xmax = 500_000.0 + 7 * 10.0
    ymax = 10_000_000.0 - 3 * 10.0
    ymin = 10_000_000.0 - 5 * 10.0
    bbox_values, bbox_transform = store.read_bbox(
        (xmin, ymin, xmax, ymax), years=[2025], crs="EPSG:32717"
    )
    assert bbox_values.shape == (1, 64, 2, 3)
    assert bbox_transform == Affine(10, 0, xmin, 0, -10, ymax)


def test_full_zarr_uses_validated_lossless_compression(tmp_path):
    path = tmp_path / "compression.zarr"
    group = _create_store(path, _grid(), [2025], "test-signature", 4, 8)
    assert group.attrs["aef:compression_protocol_version"] == COMPRESSION_PROTOCOL_VERSION
    assert group.attrs["aef:compression_codec"] == COMPRESSION_CNAME
    assert group.attrs["aef:compression_level"] == COMPRESSION_LEVEL
    assert group.attrs["aef:compression_shuffle"] == COMPRESSION_SHUFFLE
    assert group.attrs["aef:compression_typesize"] == COMPRESSION_TYPESIZE

    metadata = json.loads((path / "embeddings" / "zarr.json").read_text(encoding="utf-8"))
    inner_codecs = metadata["codecs"][0]["configuration"]["codecs"]
    codec = inner_codecs[-1]
    assert codec["name"] == "blosc"
    assert codec["configuration"] == {
        "typesize": 4,
        "cname": "zstd",
        "clevel": 7,
        "shuffle": "noshuffle",
        "blocksize": 0,
    }


def test_local_store_samples_centred_patches_with_edge_padding(tmp_path):
    path = tmp_path / "patches.zarr"
    group = _create_store(path, _grid(), [2025], "test-signature", 4, 8)
    pixels = np.arange(16 * 16, dtype=np.float32).reshape(16, 16)
    values = np.stack([pixels + band * 1_000 for band in range(64)])
    group["embeddings"][0] = values
    zarr.consolidate_metadata(str(path))
    store = AEFZarr(path)

    centre_x = 500_000.0 + (7 + 0.5) * 10.0
    centre_y = 10_000_000.0 - (6 + 0.5) * 10.0
    patch = store.sample_patches(
        [(centre_x, centre_y)], patch_size=3, years=[2025], crs="EPSG:32717"
    )
    assert patch.shape == (1, 1, 64, 3, 3)
    np.testing.assert_array_equal(patch[0, 0], values[:, 5:8, 6:9])

    edge_x = 500_000.0 + 0.5 * 10.0
    edge_y = 10_000_000.0 - 0.5 * 10.0
    edge = store.sample_patches(
        [(edge_x, edge_y)], patch_size=3, years=[2025], crs="EPSG:32717"
    )
    assert np.isnan(edge[0, 0, :, 0, :]).all()
    assert np.isnan(edge[0, 0, :, :, 0]).all()
    np.testing.assert_array_equal(edge[0, 0, :, 1:, 1:], values[:, :2, :2])

    with pytest.raises(ValueError, match="positive odd"):
        store.sample_patches(
            [(centre_x, centre_y)], patch_size=2, years=[2025], crs="EPSG:32717"
        )
