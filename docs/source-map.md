# Source migration map

This repository was extracted from the production AEF workflow in
`LL0912/DOCC_BALSA`. Project-specific imports and absolute local paths were removed.
This is historical provenance only: AEF-GRiTS does not import or read that repository.

| Current file | Former production file |
|---|---|
| `samples/aef_plantation_polygon_pixels.py` | `DOCC_BALSA/samples/aef_plantation_polygon_pixels.py` |
| `samples/aef_balsa_polygon_pixels.py` | `DOCC_BALSA/samples/aef_balsa_polygon_pixels.py` |
| `scripts/export_aef_points_ee.py` | `DOCC_BALSA/scripts/phase2_export_aef_multiyear_ee.py` |
| `scripts/download_drive_exports.py` | `DOCC_BALSA/scripts/phase2_download_drive_aef.py` |
| `scripts/merge_aef_point_exports.py` | `DOCC_BALSA/scripts/phase2_merge_aef_exports.py` |
| `scripts/aggregate_polygon_aef.py` | `DOCC_BALSA/scripts/phase2_aggregate_polygon_aef.py` |
| `scripts/build_global_background.py` | `DOCC_BALSA/scripts/phase2_build_global_aef_background.py` |
| `scripts/validate_background_exports.py` | `DOCC_BALSA/scripts/phase2_validate_global_background_aef_2025.py` |
| `scripts/export_aef_grid_ee.py` | `DOCC_BALSA/scripts/phase2_export_17mpu_aef_ee.py` |
| `scripts/build_aef_raster_catalog.py` | `DOCC_BALSA/scripts/phase2_build_aef_catalog.py` |
