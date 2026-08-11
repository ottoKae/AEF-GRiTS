# AEF-GRiTS

AEF-GRiTS is a standalone, reproducible workflow for sampling, exporting,
downloading and validating annual AlphaEarth Foundation (AEF) embeddings. It was
extracted from the production plantation-mapping pipeline in `LL0912/DOCC_BALSA`
and no longer depends on that repository.

The workflow supports two products:

1. **Point and polygon features**: deterministic points inside inventory polygons,
   annual 64-D AEF vectors, and one unit-sphere prototype per polygon and year.
2. **Wall-to-wall rasters**: annual 64-band GeoTIFF exports aligned to an existing
   reference grid, followed by VRT and catalog construction.

The Earth Engine source is `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`, with bands
`A00` through `A63`.

## Repository layout

```text
aef_grits/
├── features.py                     # AEF schema, QC and spherical operations
└── catalog.py                      # S1-GRiTS catalog/grid helpers
samples/
├── aef_plantation_polygon_pixels.py
└── aef_balsa_polygon_pixels.py
scripts/
├── export_aef_points_ee.py
├── download_drive_exports.py
├── merge_aef_point_exports.py
├── aggregate_polygon_aef.py
├── build_global_background.py
├── validate_background_exports.py
├── export_aef_grid_ee.py
└── build_aef_raster_catalog.py
docs/
├── data-layout.md
└── source-map.md
```

Downloaded CSV, Parquet, GeoTIFF and VRT products are ignored by Git. See
[`docs/data-layout.md`](docs/data-layout.md) for the recommended local layout and
the table contracts.

## Installation

The reproducible option, including GDAL for raster catalogs, is:

```bash
conda env create -f environment.yml
conda activate aef_grits
```

For point-only workflows:

```bash
python -m pip install -e ".[gee,geo,dev]"
```

Authenticate Earth Engine once and select the billing/quota project:

```bash
earthengine authenticate --auth_mode=localhost --force
earthengine set_project YOUR_GEE_PROJECT
```

Replace the example project if a different Earth Engine project owns the quota.
Credentials are read from the standard Earth Engine location and are never stored
in this repository.

## Point-to-polygon workflow

Run all commands from the repository root.

### 1. Build the deterministic plantation point registry

```bash
python samples/aef_plantation_polygon_pixels.py --shapefile /path/to/plantations.shp --out-dir outputs/plantation_inventory
```

Optionally pass `--s1-catalog /path/to/catalog.parquet` to attach the containing
S1-GRiTS grid. The principal output is:

```text
outputs/plantation_inventory/plantation_polygon_aef_points_all.csv
```

It includes the required `sample_id`, `lon` and `lat` fields. Sampling is capped at
25 interior points per polygon by default and stores reciprocal polygon weights.

### 2. Plan and submit annual point exports

Always inspect a dry run first:

```bash
python scripts/export_aef_points_ee.py --sample-csv outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --project YOUR_GEE_PROJECT --folder AEF_ALL_PLANTATION_POLYGONS --prefix plantation_polygon_pixels_2017_2025 --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --chunk-size 2000 --log-dir outputs/submitted_tasks --dry-run
```

Remove `--dry-run` to submit. The exporter writes one row per point with 64 columns
per year, such as `aef2025_center_A00` through `aef2025_center_A63`. For large jobs,
`--start-chunk` and `--max-chunks` allow controlled submission batches.

### 3. Download completed Drive exports

```bash
python scripts/download_drive_exports.py --prefix plantation_polygon_pixels_2017_2025 --out outputs/raw_exports
```

The downloader supports `.part` files, retries and resume. It verifies the expected
Google Drive file size before finalizing each file.

### 4. Validate and merge chunks

PowerShell example:

```powershell
python scripts/merge_aef_point_exports.py --master outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --exports (Get-ChildItem outputs/raw_exports/plantation_polygon_pixels_2017_2025_part*.csv | ForEach-Object FullName) --out outputs/point_features/plantation_polygon_pixels_aef.parquet
```

The merge rejects duplicate sample identifiers, incomplete annual 64-D groups and
unknown exported identifiers. A JSON QC report is written next to the output.

### 5. Aggregate annual polygon prototypes

```bash
python scripts/aggregate_polygon_aef.py --pixel-features outputs/point_features/plantation_polygon_pixels_aef.parquet --out-dir outputs/polygon_features --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --minimum-valid-fraction 1.0 --output-prefix plantation_polygon_aef
```

Each point is L2-normalized, the weighted mean direction is calculated within each
polygon, and the mean is normalized again. The complete product is:

```text
outputs/polygon_features/plantation_polygon_aef_prototypes_complete.parquet
```

## Fixed national background workflow

Create a score-independent, spatially balanced point pool outside known plantations:

```bash
python scripts/build_global_background.py --lclu /path/to/lclu.gdb --lclu-layer SC_COBERTURA_TIERRA_A --plantations /path/to/plantations.shp --verified-negatives /path/to/verified_negatives.parquet --out-dir outputs/global_background_2025 --target-points 40000
```

Export these points with `export_aef_points_ee.py`, using `--years 2025`, then
download the chunks and validate them:

```bash
python scripts/validate_background_exports.py --master outputs/global_background_2025/global_background_points_2025.csv --raw-dir outputs/global_background_2025/raw_exports --out-dir outputs/global_background_2025/features
```

## Annual raster workflow

Export a 64-band annual raster on exactly the grid of a reference GeoTIFF:

```bash
python scripts/export_aef_grid_ee.py --reference /path/to/reference.tif --project YOUR_GEE_PROJECT --prefix 17MPU_AEF --folder AEF_GRITS_RASTERS --task-log outputs/submitted_tasks/17MPU_AEF_tasks.json --dry-run
```

Remove `--dry-run` after checking the plan. Download the files using the same prefix:

```bash
python scripts/download_drive_exports.py --prefix 17MPU_AEF --out outputs/rasters/17MPU
```

Build one VRT per year and validate all grids against the reference:

```bash
python scripts/build_aef_raster_catalog.py --root outputs/rasters/17MPU --prefix 17MPU_AEF --tile-id 17MPU --reference /path/to/reference.tif --out outputs/rasters/17MPU/catalog.csv
```

## Validation

```bash
pytest
python -m compileall -q aef_grits samples scripts
```

The tests exercise the 64-D schema, year discovery, unit-sphere normalization and
deterministic spatial block assignment. Production runs additionally emit task,
merge, completeness and aggregation reports for data-level auditing.

## Provenance

The exact production-to-standalone file mapping is recorded in
[`docs/source-map.md`](docs/source-map.md). The original local data and outputs were
not copied into this repository.
