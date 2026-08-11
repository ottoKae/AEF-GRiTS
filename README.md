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
├── stream_aef_points_ee.py         # direct EE to local Parquet shards
├── stream_aef_grid_ee.py           # direct EE to local Zarr v3
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
python -m pip install -e ".[gee,geo,stream,dev]"
```

Authenticate Earth Engine once and select the billing/quota project:

```bash
earthengine authenticate --auth_mode=localhost --force
earthengine set_project YOUR_GEE_PROJECT
```

Replace the example project if a different Earth Engine project owns the quota.
Credentials are read from the standard Earth Engine location and are never stored
in this repository.

## Direct local streaming without Google Drive

Run all commands from the repository root. The preferred workflow uses two
synchronous Earth Engine Python APIs and writes every response directly to disk:

- `ee.data.computeFeatures` evaluates a `FeatureCollection`. With
  `fileFormat="PANDAS_DATAFRAME"`, the Earth Engine Python client requests all
  result pages automatically and returns one Pandas DataFrame. AEF-GRiTS then
  commits that table as an atomic Parquet shard.
- `ee.data.computePixels` evaluates one explicitly defined pixel grid and returns
  the response as a NumPy structured array when
  `fileFormat="NUMPY_NDARRAY"`. AEF-GRiTS converts its 64 named fields into a
  `[64,H,W]` float32 block and writes it into local Zarr.

These are interactive computations: no batch task, Drive folder or manual
download is created. They are ideal for point tables, pilots and bounded grid
blocks. The batch/Drive commands remain available for unusually expensive
computations that cannot finish within Earth Engine's interactive limits.

Official references:

- [Earth Engine `ee.data.computeFeatures`](https://developers.google.com/earth-engine/apidocs/ee-data-computefeatures)
- [Earth Engine `ee.data.computePixels`](https://developers.google.com/earth-engine/apidocs/ee-data-computepixels)
- [`computePixels` REST limits](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels)

### Choose the product before downloading

| Requirement | Recommended product | Command |
|---|---|---|
| Training/validation points or polygon prototypes | Parquet point shards | `stream_aef_points_ee.py` |
| Sparse candidate pixels only | Parquet point shards | `stream_aef_points_ee.py` |
| Complete annual maps or arbitrary later pixel queries | Reference-aligned Zarr | `stream_aef_grid_ee.py` |
| Request that repeatedly times out interactively | Batch export fallback | `export_aef_*_ee.py` |

Do not download full grids when an experiment only needs several thousand points.
Conversely, use Zarr when later analyses must revisit many arbitrary pixels or
create wall-to-wall maps.

### Define point downloads

The input may be CSV or Parquet and must contain:

```text
sample_id,lon,lat
```

- `sample_id` must be unique.
- `lon` and `lat` must be finite WGS84 coordinates (`EPSG:4326`).
- Additional columns such as `polygon_id`, species, tile, province and sample
  weight are retained in every output shard.
- Missing or masked AEF samples remain explicit rows with NaN features.

For a plantation inventory, first create deterministic interior points:

```bash
python samples/aef_plantation_polygon_pixels.py --shapefile /path/to/plantations.shp --out-dir outputs/plantation_inventory
```

Optionally pass `--s1-catalog /path/to/catalog.parquet` to attach the containing
S1-GRiTS grid. The principal point table is
`outputs/plantation_inventory/plantation_polygon_aef_points_all.csv`.

### Stream point features

```bash
python scripts/stream_aef_points_ee.py --samples outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --out-dir outputs/point_stream --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --chunk-size 1000 --workers 1
```

Output is resumable and partitioned:

```text
outputs/point_stream/
├── run.json
├── catalog.parquet
├── report.json
└── shards/
    ├── part00001.parquet
    └── ...
```

Use one worker initially because point sampling is an Earth Engine aggregation.
Increase to two only after a representative timing test is stable.

`--chunk-size` controls the local resume unit and the number of coordinates placed
in one Earth Engine expression. `--page-size` controls the internal server response
page size; it does not change the final Parquet partitioning. For nine years, each
row contains 576 feature columns:

```text
aef2017_center_A00 ... aef2017_center_A63
...
aef2025_center_A00 ... aef2025_center_A63
```

### Define and stream a reference-aligned grid

Grid downloads require a reference GeoTIFF. Its metadata is the complete grid
contract:

```text
CRS + affine transform + width + height
```

The reference should normally be the Stage-1 S1-GRiTS candidate or logit raster
for the same tile. This guarantees identical pixel centres, dimensions and
per-tile CRS (`EPSG:32717` or `EPSG:32617`). A bounding box alone is insufficient
because it does not lock the pixel origin. A 30 m reference produces 30 m AEF
output; a 10 m reference creates approximately nine times as many spatial pixels.

The implementation preserves AEF vectors with Earth Engine's default
nearest-neighbour reprojection rather than interpolating embedding dimensions.

```bash
python scripts/stream_aef_grid_ee.py --reference /path/to/17MPU_reference.tif --out outputs/17MPU/aef/zarr/aef_17MPU_2017_2025.zarr --catalog outputs/catalog.parquet --tile-id 17MPU --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --block-size 256 --workers 2
```

The default request is `256 x 256 x 64 x float32`, or 16.8 MB uncompressed,
comfortably below the Earth Engine 48 MB `computePixels` limit. The local cube is:

```text
embeddings(time, band, y, x) float32
chunks=(1, 64, 32, 32)
shards=(1, 64, 512, 512)
```

Small inner chunks make point sampling inexpensive, while larger Zarr v3 shards
avoid millions of tiny filesystem objects. A progress ledger is committed during
the run, so an interrupted command can be rerun with the same arguments.

The run signature binds the source asset, years, reference grid, request block,
inner chunk and shard size. Reusing an output path with a different contract is
rejected instead of silently mixing grids.

### Read the local Zarr stores

```python
from aef_grits.store import AEFZarr, AEFCatalog

tile = AEFZarr("outputs/17MPU/aef/zarr/aef_17MPU_2017_2025.zarr")
values = tile.sample_points([(-79.30, -1.10)], years=[2024, 2025])
# values.shape == (1, 2, 64)

catalog = AEFCatalog("outputs/catalog.parquet")
values = catalog.sample_points([(-79.30, -1.10), (-80.10, 0.20)], years=[2025])
```

`AEFZarr.sample_points()` returns `[N,T,64]`, groups points by touched 32 x 32
chunk, and reads each touched chunk once. Points outside the store return NaN.
`AEFCatalog` routes points across multiple tile stores; pass explicit `tile_ids`
when points lie in overlapping tile margins. For lazy regional processing:

```python
ds = tile.open_xarray()
print(ds.embeddings.dims)  # ('time', 'band', 'y', 'x')
```

### Performance guidance

Measured on the current machine and Earth Engine project:

- 1,000 real points x 9 years x 64 dimensions: 16.1 seconds, all rows complete.
- One `256 x 256 x 64` grid block: 27.3 seconds and about 10.9 MB compressed.
- Resume checks reuse completed point shards and grid blocks without downloading
  them again.

Point performance is already adequate for current sample-scale experiments. Full
11-tile grids still justify tuning. Before production, benchmark one tile with
`--block-size` 256 versus 384 and `--workers` 1, 2 and 4. Keep a setting only if
throughput improves without HTTP 429, timeout or aggregation errors. Do not enable
high-volume mode for point aggregations by default; it is intended primarily for
many simple pixel requests.

The largest performance gain is spatial selectivity, not extra concurrency. If
Stage 2 only classifies Stage-1 candidate pixels, convert those pixel centres into
a point table and use the point streamer. Download a complete tile cube only when
wall-to-wall visualization, repeated arbitrary queries or future reuse justifies
the additional transfer and storage.

For example, if the Stage-1 candidate area occupies 5% of all tile pixels, a
sparse point download requests and stores only about 5% of the embedding values.
The theoretical reduction in AEF transfer and feature storage is therefore about
95%. The observed reduction will be slightly smaller because Parquet metadata,
request setup and catalogs have fixed overhead. Download a complete Zarr cube only
when the project needs a full AEF spatial image, repeated queries at arbitrary
pixels, wall-to-wall diagnostics, or long-term reuse beyond the current candidate
mask.

At approximately 3667 x 3667 pixels, one nine-year 30 m tile contains about 31 GB
of uncompressed AEF values. The pilot compression suggests roughly 19-21 GB per
tile, but the actual value is data-dependent. A national 11-tile download should
therefore follow a complete 17MPU timing and storage pilot.

### Required benchmark before an 11-tile grid run

Use a fixed 17MPU reference grid, the same Earth Engine project and the same local
disk for every run. Record wall time, successful blocks per second, retries,
HTTP 429/timeouts, valid-pixel counts and final bytes on disk.

1. **Point baseline**: download a fixed 10,000-point table for all nine years.
   Compare `chunk-size` 1000/2000 and `workers` 1/2. Confirm identical sample IDs,
   feature completeness and numerical values.
2. **Grid microbenchmark**: use a fixed 1024 x 1024 crop aligned to the 17MPU
   reference. Test `(block-size, workers)` = `(256,1)`, `(256,2)`, `(384,2)` and
   `(384,4)`. Test the high-volume endpoint only for `computePixels`, after the
   standard endpoint baseline.
3. **Sparse-versus-grid parity**: sample fixed candidate pixel centres through the
   point route and the corresponding completed Zarr. Measure exact equality and
   maximum absolute/cosine difference before adopting sparse extraction for maps.
4. **Full-tile single-year test**: stream 17MPU for 2025 using the fastest stable
   configuration. Verify its CRS, affine transform, dimensions, 64 bands, valid
   mask and disk size.
5. **Resume test**: interrupt a grid run after several blocks, rerun the identical
   command, and confirm completed blocks are reused and the final sampled values
   match a clean run.
6. **Full 17MPU nine-year test**: run 2017-2025 only after steps 1-5 pass. Use its
   observed time and storage, rather than a linear microbenchmark estimate, to
   approve or reject the 11-tile production run.

The selected production configuration must prioritize completeness and stable
resume over the highest short-run throughput. A faster setting that causes
intermittent missing blocks, quota failures or numerical mismatches is rejected.

## Batch/Google Drive fallback

Use this route only when the direct interactive request repeatedly times out or
when an organizational workflow specifically requires batch exports.

### Plan and submit annual point exports

Always inspect a dry run first:

```bash
python scripts/export_aef_points_ee.py --sample-csv outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --project YOUR_GEE_PROJECT --folder AEF_ALL_PLANTATION_POLYGONS --prefix plantation_polygon_pixels_2017_2025 --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --chunk-size 2000 --log-dir outputs/submitted_tasks --dry-run
```

Remove `--dry-run` to submit. The exporter writes one row per point with 64 columns
per year, such as `aef2025_center_A00` through `aef2025_center_A63`. For large jobs,
`--start-chunk` and `--max-chunks` allow controlled submission batches.

### Download completed Drive exports

```bash
python scripts/download_drive_exports.py --prefix plantation_polygon_pixels_2017_2025 --out outputs/raw_exports
```

The downloader supports `.part` files, retries and resume. It verifies the expected
Google Drive file size before finalizing each file.

### Validate and merge chunks

PowerShell example:

```powershell
python scripts/merge_aef_point_exports.py --master outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --exports (Get-ChildItem outputs/raw_exports/plantation_polygon_pixels_2017_2025_part*.csv | ForEach-Object FullName) --out outputs/point_features/plantation_polygon_pixels_aef.parquet
```

The merge rejects duplicate sample identifiers, incomplete annual 64-D groups and
unknown exported identifiers. A JSON QC report is written next to the output.

## Aggregate annual polygon prototypes

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
