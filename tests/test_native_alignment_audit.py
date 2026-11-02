import numpy as np
from affine import Affine

from tooling.diagnostics.native_alignment_20261101.audit import compare, grid_relation
from tooling.diagnostics.native_alignment_20261101.finish_audit import footprint_relation


def test_alignment_requires_integer_offsets_and_matching_crs():
    source = Affine(10, 0, 500000, 0, -10, 1000)
    assert grid_relation("EPSG:32631", source, "EPSG:32631",
                         source * Affine.translation(4, -2))["aligned"]
    shifted = grid_relation("EPSG:32631", source, "EPSG:32631",
                            Affine(10, 0, 500003, 0, -10, 997))
    assert not shifted["aligned"]
    np.testing.assert_allclose(shifted["fractional_pixel_residual"], [.3, .3])
    assert not grid_relation("EPSG:32631", source, "EPSG:32632", source)["aligned"]


def test_comparison_reports_missing_vectors_without_dropping_them():
    left = np.ones((3, 64), np.float32)
    left[0] = 0
    right = left.copy()
    right[1] = np.nan
    right[2, 0] = 2
    result = compare(left, right)
    assert result["total_vectors"] == 3
    assert result["complete_pairs"] == 2
    assert result["exact_vectors"] == 1
    assert result["mask_disagreements"] == 64
    assert result["max_abs_difference"] == 1


def test_footprints_allow_lossless_axis_flip_but_not_fractional_shift():
    source = Affine(10, 0, 500000, 0, 10, 1000)
    target = Affine(10, 0, 500020, 0, -10, 1200)
    result = footprint_relation("EPSG:32631", source, "EPSG:32631", target)
    assert result["same_pixel_footprints"]
    assert not result["same_array_orientation"]
    assert not footprint_relation("EPSG:32631", source, "EPSG:32631",
                                  Affine(10, 0, 500023, 0, -10, 1200))["same_pixel_footprints"]
    assert not footprint_relation("EPSG:32631", source, "EPSG:32632", target)["same_pixel_footprints"]
