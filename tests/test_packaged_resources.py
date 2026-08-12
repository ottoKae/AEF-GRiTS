from hashlib import sha256

import pandas as pd

from aef_grits.grids import MGRSGridProvider
from aef_grits.resources import BUILTIN_MGRS_INDEX, mgrs_index_path


EXPECTED_MGRS_SHA256 = (
    "5c1ebd1f234ae74d9c249d91cb9c269eb6defa37a901da0c7bbcb7e50523fc6b"
)


def test_packaged_mgrs_index_is_complete_and_stable():
    path = mgrs_index_path()
    assert path == BUILTIN_MGRS_INDEX.resolve()
    assert sha256(path.read_bytes()).hexdigest() == EXPECTED_MGRS_SHA256
    frame = pd.read_parquet(path, columns=["mgrs_tile_id", "utm_epsg"])
    assert len(frame) == 19_002
    assert frame.mgrs_tile_id.is_unique
    assert {"17MPU", "50RLU", "50SQB"}.issubset(set(frame.mgrs_tile_id))


def test_mgrs_provider_needs_no_external_path():
    grid = MGRSGridProvider().get("17MPU")
    assert grid.grid_id == "17MPU"
    assert grid.metadata["mgrs_parquet"] == str(BUILTIN_MGRS_INDEX.resolve())
    assert grid.width == 10_980
    assert grid.height == 10_980
