import copy
import sys

from affine import Affine
import pytest

from aef_grits.native_grid import NativeAlignmentError, NativeSource, pixel_footprints_match, source_manifest


def source_info():
    return {"id": "source_a", "version": 1, "properties": {"DATASET_VERSION": "1.1"},
            "bands": [{"id": f"A{i:02d}", "crs": "EPSG:32649",
                       "crs_transform": [10, 0, 663840, 0, 10, 2457600],
                       "dimensions": [16384, 16384]} for i in range(64)]}


def test_native_metadata_and_lossless_row_reversal():
    source = NativeSource.from_info(source_info(), 2025)
    target = Affine(10, 0, 663860, 0, -10, 2457800)
    assert pixel_footprints_match(source.crs, source.transform, source.crs, target)
    assert not pixel_footprints_match(source.crs, source.transform, "EPSG:32650", target)
    assert not pixel_footprints_match(source.crs, source.transform, source.crs,
                                      Affine(10, 0, 663863, 0, -10, 2457800))


def test_reject_mixed_band_grids():
    info = source_info()
    info["bands"][63]["crs_transform"][2] += 3
    with pytest.raises(NativeAlignmentError, match="All 64"):
        NativeSource.from_info(info, 2025)


def test_reject_unversioned_source():
    info = source_info()
    del info["version"]
    with pytest.raises(NativeAlignmentError, match="asset version"):
        NativeSource.from_info(info, 2025)


def test_manifest_freezes_asset_version_and_order():
    first = NativeSource.from_info(source_info(), 2025)
    second_info = copy.deepcopy(source_info())
    second_info["id"] = "source_b"
    second = NativeSource.from_info(second_info, 2025)
    assert source_manifest([first, second]) == source_manifest([second, first])
    second_info["version"] = 2
    changed = NativeSource.from_info(second_info, 2025)
    assert source_manifest([second])["signature"] != source_manifest([changed])["signature"]


def test_legacy_drive_point_submission_rejected_before_io(monkeypatch):
    from scripts import export_aef_points_ee

    monkeypatch.setattr(sys, "argv", ["export", "--sample-csv", "not_read.csv",
                                     "--log-dir", "not_created"])
    with pytest.raises(ValueError, match="stream_aef_points_ee"):
        export_aef_points_ee.main()
