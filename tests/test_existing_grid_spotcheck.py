import numpy as np

from tooling.diagnostics.existing_grids_20261101.spotcheck import classify


def test_spotcheck_does_not_call_missing_or_different_values_native():
    values = np.ones((16, 16, 64), np.float32)
    missing = np.full_like(values, np.nan)
    assert classify(values, values.copy(), []) == 'sample_native_match'
    assert classify(missing, missing, []) == 'inconclusive_local_nodata'
    assert classify(values, missing, []) == 'inconclusive_native_nodata'
    assert classify(values, values * 2, []) == 'unresolved_difference_not_automatically_resampling'
    assert classify(values, values * 2, [values.copy()]) == 'cross_utm_source_match_at_differing_pixels'
