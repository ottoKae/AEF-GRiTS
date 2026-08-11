import numpy as np
from affine import Affine
import zarr

from aef_grits.store import AEFZarr
from scripts.stream_aef_grid_ee import _create_store, _pixel_grid


def _grid():
    return {
        "crs": "EPSG:32717",
        "transform": Affine(30.0, 0.0, 500_000.0, 0.0, -30.0, 10_000_000.0),
        "width": 16,
        "height": 16,
        "bounds": (500_000.0, 9_999_520.0, 500_480.0, 10_000_000.0),
        "tile_id": "TEST",
    }


def test_compute_pixels_grid_is_window_aligned():
    block = {"row": 3, "column": 4, "width": 5, "height": 6}
    request_grid = _pixel_grid(_grid(), block)
    affine = request_grid["affineTransform"]
    assert request_grid["dimensions"] == {"width": 5, "height": 6}
    assert affine["translateX"] == 500_120.0
    assert affine["translateY"] == 9_999_910.0
    assert affine["scaleX"] == 30.0
    assert affine["scaleY"] == -30.0


def test_local_store_samples_touched_chunks(tmp_path):
    path = tmp_path / "test.zarr"
    group = _create_store(path, _grid(), [2024, 2025], "test-signature", 4, 8)
    values = np.arange(64, dtype=np.float32)
    group["embeddings"][1, :, 6, 7] = values
    zarr.consolidate_metadata(str(path))

    store = AEFZarr(path)
    x = 500_000.0 + (7 + 0.5) * 30.0
    y = 10_000_000.0 - (6 + 0.5) * 30.0
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
