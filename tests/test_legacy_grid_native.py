"""The retained Drive exporter must not bypass native-grid validation."""

from affine import Affine
import pytest

from aef_grits.native_grid import NativeAlignmentError, NativeSource
from scripts import export_aef_grid_ee as exporter


@pytest.mark.parametrize(
    "target_crs,target_transform",
    [
        ("EPSG:32632", Affine(10, 0, 500000, 0, -10, 1000)),
        ("EPSG:32631", Affine(10, 0, 500003, 0, -10, 1000)),
    ],
)
def test_drive_export_rejects_incompatible_native_source(
    monkeypatch, target_crs, target_transform
):
    source = NativeSource(
        "source", 2025, "EPSG:32631", Affine(10, 0, 500000, 0, -10, 1000),
        8, 8, 1, {},
    )
    monkeypatch.setattr(exporter, "iter_native_sources", lambda *args: iter([source]))
    with pytest.raises(NativeAlignmentError, match="No native AEF source"):
        exporter.annual_image(
            2025, object(),
            {"crs": target_crs, "crs_transform": list(target_transform)[:6]},
        )
