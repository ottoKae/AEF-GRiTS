import numpy as np

from aef_grits.catalog import spatial_block_id
from aef_grits.features import discover_years, feature_columns, l2_normalize


def test_feature_schema_and_year_discovery():
    columns = feature_columns(2025)
    assert len(columns) == 64
    assert columns[0] == "aef2025_center_A00"
    assert columns[-1] == "aef2025_center_A63"
    assert discover_years(columns) == [2025]


def test_l2_normalize_preserves_unit_geometry():
    values = np.array([[3.0, 4.0], [0.0, 2.0]])
    normalized = l2_normalize(values)
    np.testing.assert_allclose(np.linalg.norm(normalized, axis=1), 1.0)


def test_spatial_block_is_deterministic():
    assert spatial_block_id(12_001.0, 17_999.0, 6_000.0) == "g2_2"
