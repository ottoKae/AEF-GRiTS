# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

AEF-GRiTS is a standalone, reproducible workflow for sampling, exporting,
downloading and validating annual AlphaEarth Foundation (AEF) embeddings.

The workflow supports four download routes:

1. **Point features**: tabular or vector samples are converted to WGS84 points
   and streamed to Parquet shards.
2. **Reference grids**: one 10 m Zarr follows an arbitrary 10 m reference GeoTIFF.
3. **Tessera 0.1-degree grids**: one 10 m UTM Zarr per Tessera-style small tile.
4. **MGRS grids**: one 10 m Zarr per grid defined by the S1-GRiTS MGRS table.

Polygon sampling and unit-sphere prototype aggregation build on the point route.

The Earth Engine source is `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`, with bands
`A00` through `A63`.

## Repository layout

```text
aef_grits/
├── features.py                     # AEF schema, QC and spherical operations
├── catalog.py                      # S1-GRiTS catalog/grid helpers
├── earth_engine.py                 # shared Earth Engine image builders
├── atomic.py                       # atomic JSON and Parquet writers
├── grids.py                        # fixed 10 m grid contracts and providers
├── points.py                       # table/vector conversion and preflight checks
├── point_store.py                  # validated point-shard loader
├── grid_lookup.py                  # AOI-to-MGRS/Tessera spatial lookup
└── store.py                        # Zarr and multi-grid catalog reader
samples/
├── aef_plantation_polygon_pixels.py
└── aef_balsa_polygon_pixels.py
scripts/
├── stream_aef_points_ee.py         # direct EE to local Parquet shards
├── stream_aef_grid_ee.py           # direct EE to local Zarr v3
├── prepare_aef_points.py           # vector/polygon to canonical point table
├── extract_aef_patches.py          # Zarr catalog to sharded NPY tensors
├── merge_aef_point_shards.py       # validated point-shard merge
├── smoke_test_aef_tiles.py         # bounded Tessera/MGRS live probes
├── resolve_aef_grid_ids.py         # vector AOI to fixed grid-ID lists
├── search_aef_grid_catalog.py      # bilingual region/grid search
├── visualize_aef_grid_lookup.py    # MGRS/Tessera quick-look maps
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
├── direct-streaming.md
└── source-map.md
```

Downloaded CSV, Parquet, GeoTIFF, VRT and Zarr products are ignored by Git. See
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

| Download route | Earth Engine API | Local format | Intended use |
|---|---|---|---|
| Points | `computeFeatures` | Parquet shards | Training points, polygon centres and sparse candidate pixels |
| `reference` | `computePixels` | Zarr v3 | Local experiments and custom 10 m reference areas |
| `tessera_0p1` | `computePixels` | One Zarr v3 per 0.1-degree tile | Small downloads, pilots and Tessera comparison |
| `mgrs` | `computePixels` | One Zarr v3 per MGRS tile | Pixel-aligned S1/AEF fusion and production mapping |

Do not download full grids when an experiment only needs several thousand points.
Conversely, use Zarr when later analyses must revisit many arbitrary pixels or
create wall-to-wall maps.

### Define point downloads

#### Minimum input requirements

| Input | Required content | CRS rule | Sample ID |
|---|---|---|---|
| CSV | `sample_id,lon,lat` | `lon/lat` must be WGS84 (`EPSG:4326`) | `sample_id` must be unique |
| Shapefile | Point/MultiPoint or Polygon/MultiPolygon geometry | A valid `.prj` is required; AEF-GRiTS converts its declared CRS to WGS84 | Use an existing unique field with `--id-field`; otherwise deterministic IDs are generated |

Additional label, species, split, polygon and administrative fields are retained.
For polygons, the default is one guaranteed-interior representative point per
feature. Use `--geometry-mode interior_pixels --reference-grid <GeoTIFF-or-Zarr>`
only when every grid-aligned interior pixel is required. Never place projected
`x/y` coordinates in CSV columns named `lon/lat`; CSV has no embedded CRS, so
projected tables must first be converted to WGS84.

CSV example:

```csv
sample_id,lon,lat,label,split
p0001,116.287048,39.533912,1,train
p0002,114.409640,35.472660,0,validation
```

```bash
python scripts/stream_aef_points_ee.py --samples data/points.csv --out-dir outputs/points_2020 --project YOUR_GEE_PROJECT --years 2020
```

Shapefile example:

```bash
python scripts/stream_aef_points_ee.py --samples data/samples.shp --id-field id --out-dir outputs/points_2020 --project YOUR_GEE_PROJECT --years 2020
```

#### Why points use WGS84 and grids use UTM

AEF feature vectors do not themselves have a CRS; the CRS belongs to the image
grid that locates each vector. Earth Engine AEF source images use local UTM zones,
not one global UTM projection. A cross-country point table may consequently span
many source projections.

AEF-GRiTS uses WGS84 (`EPSG:4326`) as the canonical point exchange CRS because:

- one longitude/latitude pair identifies a location globally;
- one table can cross UTM zones without storing a different EPSG code per row;
- CSV, GIS and Earth Engine interfaces exchange WGS84 points consistently; and
- Earth Engine transforms each point into the relevant AEF image projection when
  `sampleRegions` performs the 10 m sample.

The point Parquet output therefore retains WGS84 `lon/lat`; its 64-dimensional
AEF vector is a feature value rather than a projected geometry. A vector input in
another declared CRS is converted to WGS84 before submission. A projected CSV is
never guessed because CSV does not carry authoritative CRS metadata.

Dense grids have the opposite requirement. A wall-to-wall Zarr needs a constant
metric pixel size, an unambiguous pixel origin and an affine row/column mapping.
Geographic degrees cannot provide a constant 10 m spacing: the ground distance of
one longitude degree changes with latitude. Each complete grid is therefore stored
in its appropriate local UTM zone:

- pixels remain square and 10 m in metric units;
- `CRS + affine transform + width + height` uniquely identifies every pixel;
- MGRS boundaries and S1-GRiTS pixels can be aligned directly; and
- window, patch and wall-to-wall inference operate on integer rows and columns
  without repeated geographic reprojection.

The two contracts meet in the resolver:

```text
point table: WGS84 lon/lat
        ↓ Earth Engine or PyProj coordinate transformation
local AEF/Zarr UTM coordinates
        ↓ inverse affine transform
pixel row and column → 64-D AEF vector
```

For a WGS84 point sampled from a local Zarr, `AEFZarr` performs this conversion
automatically. In overlapping tile margins, pass an explicit `grid_id` so the
intended UTM grid and pixel origin are deterministic. When point-to-Zarr bit parity
is required, construct the point from the target Zarr pixel centre rather than an
arbitrary coordinate near a pixel boundary.

The point input may be CSV, Parquet, Shapefile, GeoPackage or GeoJSON. A table
must contain one row per requested point:

```text
sample_id,lon,lat
```

- `sample_id` must be unique.
- `lon` and `lat` must be finite WGS84 coordinates (`EPSG:4326`).
- Additional columns such as `polygon_id`, species, tile, province and sample
  weight are retained in every output shard.
- Missing or masked AEF samples remain explicit rows with NaN features.
- Point sampling is fixed at the native 10 m AEF scale. A different `--scale`
  is rejected; coarser comparisons must be created downstream.

Point vector features are kept directly. Polygon and MultiPolygon features use
one guaranteed-interior representative point by default. Select another explicit
policy with `--geometry-mode`:

- `representative`: one interior point per polygon; recommended for inventories.
- `centroid`: one centroid, which can fall outside a concave polygon.
- `interior_pixels`: every pixel centre inside the polygon. This mode requires
  `--reference-grid` pointing to a GeoTIFF or AEF Zarr so centres exactly follow
  an authoritative grid, and is protected by `--max-points`.

For GeoPackage input, use `--layer`; use `--id-field polygon_id` (or another
stable field) to define sample IDs. Existing candidate-pixel CSV/Parquet tables
can be supplied directly without conversion.

For example, create a reproducible 10,000-point benchmark from an existing point
registry as follows. Random sampling is suitable for a throughput benchmark;
model training should use the project's spatially stratified sampling protocol.

```python
import pandas as pd

source = pd.read_parquet("data/all_candidate_points.parquet")  # CSV also works
points = source.sample(n=10_000, random_state=20260811)
points[["sample_id", "lon", "lat"]].to_parquet(
    "data/aef_benchmark_10000.parquet", index=False
)
```

Every `sample_id` must remain unique after selection. The downloader does not
choose the 10,000 points; it extracts exactly the rows supplied by the user.

To create a reusable canonical table without downloading AEF yet:

```bash
python scripts/prepare_aef_points.py --input data/plantations.gpkg --layer plantations --id-field polygon_id --geometry-mode representative --output outputs/prepared_points/plantations.parquet
```

To enumerate polygon-internal centres on an existing AEF/MGRS grid:

```bash
python scripts/prepare_aef_points.py --input data/plantations.shp --id-field polygon_id --geometry-mode interior_pixels --reference-grid outputs/mgrs/17MPU/aef_17MPU_2017_2025.zarr --output outputs/prepared_points/plantation_pixels.parquet
```

For a plantation inventory, first create deterministic interior points:

```bash
python samples/aef_plantation_polygon_pixels.py --shapefile /path/to/plantations.shp --out-dir outputs/plantation_inventory
```

Optionally pass `--s1-catalog /path/to/catalog.parquet` to attach the containing
S1-GRiTS grid. The principal point table is
`outputs/plantation_inventory/plantation_polygon_aef_points_all.csv`.

### Stream point features

`--out-dir` explicitly selects the storage location. If omitted, the default is
`<repository>/outputs/point_stream`, regardless of the shell's current directory.
An absolute path can place the product on another disk.

Run preflight without Earth Engine authentication or data requests:

```bash
python scripts/stream_aef_points_ee.py --samples data/aef_points.parquet --out-dir D:/AEF/points_2017_2025 --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --validate-only
```

`validation_report.json` checks empty input, required columns, finite and valid
longitude/latitude ranges, duplicate IDs, duplicate coordinate groups, year
range, feature count, expected shard count, and available split/species/tile
group counts. Duplicate coordinates are reported as a warning; duplicate IDs and
invalid coordinates are errors.

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

Each Parquet shard retains all input metadata columns, appends the requested AEF
feature columns, adds one annual completeness flag and adds
`aef_complete_all_years`. `catalog.parquet` contains one record per shard;
`report.json` summarizes complete and incomplete rows. Point downloads are not
stored in Zarr.

Load and validate every shard through one API:

```python
from aef_grits import load_aef_points

dataset = load_aef_points("outputs/point_stream", years=[2024, 2025])
points = dataset.frame
print(dataset.report)
```

The loader rejects missing files, mixed signatures, row-count mismatches,
duplicate sample IDs, missing years and incomplete 64-dimensional annual schemas.
Pass `require_complete=True` to reject any NaN row. To materialize one validated
Parquet table:

```bash
python scripts/merge_aef_point_shards.py --catalog outputs/point_stream/catalog.parquet --output outputs/point_features/aef_points.parquet --require-complete
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

### Define and stream 10 m grids

The CRS contract is deliberately simple:

| Object | CRS/storage rule |
|---|---|
| AEF vector | No CRS by itself; it is 64 feature values |
| Point request/result | WGS84 longitude/latitude (`EPSG:4326`) |
| Dense Zarr | The grid's local UTM CRS, 10 m square pixels |
| MGRS/Tessera ID | A discovery key; read the Zarr metadata for the exact CRS and transform |

Earth Engine transforms WGS84 requests to the source image internally. Dense
arrays use UTM because geographic degrees cannot define a constant 10 m pixel.

In `reference` mode, grid downloads require a reference GeoTIFF. Its metadata is
the complete grid contract:

```text
CRS + affine transform + width + height
```

The reference must be projected, north-up and exactly 10 m. This guarantees the
native AEF spatial contract. A bounding box alone is insufficient because it does
not lock the pixel origin. A non-10 m reference is rejected; any 30 m comparison
must be generated later as a separate, explicitly documented product.

The implementation preserves AEF vectors with Earth Engine's default
nearest-neighbour reprojection rather than interpolating embedding dimensions.

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme reference --reference /path/to/10m_reference.tif --out outputs/reference/aef_custom_2017_2025.zarr --catalog outputs/reference/catalog.parquet --tile-id custom_area --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --block-size 256 --workers 2
```

The Tessera-style provider uses 0.1-degree cells centred on 0.05-degree offsets,
with names such as `grid_-79.95_-1.05`. The 0.1-degree cell defines discovery and
identity; the stored pixels use a snapped 10 m local UTM grid. Select explicit
tile centres, repeating the option as needed:

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme tessera_0p1 --tessera-tile -79.95 -1.05 --out outputs/tessera_0p1/grid_-79.95_-1.05/aef_2017_2025.zarr --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025
```

Or enumerate every 0.1-degree cell intersecting a WGS84 bounding box:

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme tessera_0p1 --bbox -80.0 -1.2 -79.7 -0.9 --out-dir outputs/tessera_0p1 --project YOUR_GEE_PROJECT --years 2025
```

The MGRS provider reads the authoritative S1-GRiTS table. It uses `utm_epsg` and
`utm_wkt`, snaps the supplied projected bounds to the 10 m lattice and does not
infer tile geometry from the tile name:

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme mgrs --mgrs-index D:/Project/claude-demo/S1-GRiTS/src/s1grits/data/mgrs.parquet --tiles 17MNT 17MNV 17MPT 17MPU 17MPV 17NQA --out-dir outputs/mgrs --catalog outputs/mgrs/catalog.parquet --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025
```

#### Convert an AOI to grid IDs

`resolve_aef_grid_ids.py` accepts Shapefile, GeoPackage, GeoJSON or GeoParquet,
uses the input's declared CRS, and returns both MGRS and Tessera 0.1-degree IDs.
For polygon AOIs it selects all cells with a positive-area intersection; a grid
that only touches the boundary is excluded by default. For point AOIs it returns
only the cells containing those points. Tessera cells are non-overlapping and
therefore deterministic for a point. S1-GRiTS MGRS coverage intentionally overlaps
near UTM-zone and tile margins, so one point may legitimately list more than one
MGRS store; retain all for area coverage or choose the intended store explicitly
when extracting a point.

```powershell
python scripts/resolve_aef_grid_ids.py `
  --aoi data/provinces.gpkg --layer provinces `
  --region-id-field province_code `
  --name-field-cn province_cn --name-field-en province_en `
  --mgrs-index D:/Project/claude-demo/S1-GRiTS/src/s1grits/data/mgrs.parquet `
  --out-dir outputs/grid_lookup/provinces
```

Outputs include `mgrs_grid_ids.txt`, `tessera_0p1_grid_ids.txt`, a per-region
`grid_intersections.parquet`, `grid_ids.json`, and a bilingual searchable
`grid_catalog.parquet`. Province names are copied from the user-specified
administrative-boundary fields; they are not guessed from MGRS codes. This keeps
geometry intersection authoritative while allowing Chinese, English, official
codes and aliases to be searched:

```powershell
python scripts/search_aef_grid_catalog.py `
  --catalog outputs/grid_lookup/provinces/grid_catalog.parquet `
  --query "Santo Domingo" --scheme mgrs --ids-only

$tiles = Get-Content outputs/grid_lookup/provinces/mgrs_grid_ids.txt
python scripts/stream_aef_grid_ee.py --grid-scheme mgrs `
  --mgrs-index D:/Project/claude-demo/S1-GRiTS/src/s1grits/data/mgrs.parquet `
  --tiles $tiles --out-dir outputs/mgrs --project YOUR_GEE_PROJECT --years 2025
```

Create a quick-look PNG, SVG and JSON report before downloading imagery:

```powershell
python scripts/visualize_aef_grid_lookup.py `
  --intersections outputs/grid_lookup/provinces/grid_intersections.parquet `
  --aoi data/provinces.gpkg --layer provinces `
  --out outputs/grid_lookup/provinces/grid_coverage.png `
  --title "Province AEF grid lookup"
```

The recommended agent-assisted workflow is intentionally staged:

1. Give the agent the absolute AOI path, optional GeoPackage layer, region ID,
   Chinese/English name fields, desired schemes, years, Earth Engine project and
   output directory.
2. First request **lookup and visualization only**. Ask for the selected grid
   counts and lists; explicitly say not to start an Earth Engine download.
3. Request a bounded pilot for one explicit grid and one year. Review its CRS,
   10 m shape, completeness, elapsed time and storage.
4. Only then authorize a named list of grids and years. For a province-scale job,
   ask the agent to print the exact command and size estimate before execution.

Example first message to an agent:

```text
In AEF-GRiTS, resolve this AOI to both MGRS and Tessera 0.1-degree grids:
AOI=D:/data/provinces.gpkg, layer=provinces,
region ID=province_code, Chinese name=province_cn, English name=province_en.
Use D:/Project/claude-demo/S1-GRiTS/src/s1grits/data/mgrs.parquet.
Write to outputs/grid_lookup/provinces and make the coverage quick-look.
Report all counts and IDs, but do not download AEF yet.
```

Then authorize a bounded pilot separately:

```text
Using the generated catalog, download only MGRS 50SNA for 2025 with
Earth Engine project YOUR_GEE_PROJECT. Report runtime, Zarr shape, CRS,
valid fraction and disk size. Do not expand to the other grids.
```

#### Anhui validation example

The real Anhui ADM1 polygon selected 28 MGRS grids (all EPSG:32650) and
1,477 Tessera 0.1-degree cells. Chinese query `安徽省`, English query
`Anhui Province`, and code query `CN-AH` returned the same grid sets; the catalog
had no duplicate `(scheme, grid_id)` rows. This test validated lookup and
visualization only—no province-wide AEF download was started. See
[`docs/anhui-grid-lookup-example.md`](docs/anhui-grid-lookup-example.md) for the
commands, complete MGRS list and interpretation.

Multi-grid output is organized as follows:

```text
outputs/mgrs/
├── catalog.parquet
├── run_summary.json
├── 17MPU/
│   ├── aef_17MPU_2017_2025.zarr
│   ├── aef_17MPU_2017_2025.zarr.progress.json
│   └── aef_17MPU_2017_2025.zarr.report.json
└── ...
```

The default request is `256 x 256 x 64 x float32`, or 16.8 MB uncompressed,
comfortably below the Earth Engine 48 MB `computePixels` limit. The local cube is:

```text
embeddings(time, band, y, x) float32
chunks=(1, 64, 64, 64)
shards=(1, 64, 512, 512)
codec=Blosc(Zstd level 7, no shuffle, typesize 4)
```

The 64 x 64 inner chunk is the selected balance for point, patch and dense reads,
while 512 x 512 Zarr v3 shards avoid millions of tiny filesystem objects. A progress ledger is committed during
the run, so an interrupted command can be rerun with the same arguments.

The codec is lossless: decoded values retain the original float32 bit patterns.
It is the version-2 storage protocol and is recorded in both Zarr attributes and
the Parquet catalog. Validation used 2017, 2021 and 2025 blocks from four spatially
separated MGRS tiles (17MNT, 17MPU, 17MPV and 17NQA). All 12 block comparisons and
all four three-year Zarr round trips were bit-exact. The candidate stores occupied
17.9%-19.8% of raw float32 bytes (median 19.2%) and were 63.1%-63.7% smaller than
the former Zstd-3 byte-shuffle stores. These figures are pilot measurements, not
a guarantee for every complete tile.

The run signature binds the source asset, years, reference grid, request block,
inner chunk, shard size and compression protocol. Reusing an output path with a
different contract is rejected instead of silently mixing grids. Existing stores
remain readable, but an incomplete store created under the former compression
protocol must be completed with the old code or restarted at a new output path.

### Read the local Zarr stores

```python
from aef_grits.store import AEFZarr, AEFCatalog

tile = AEFZarr("outputs/17MPU/aef/zarr/aef_17MPU_2017_2025.zarr")
values = tile.sample_points([(-79.30, -1.10)], years=[2024, 2025])
# values.shape == (1, 2, 64)

patches = tile.sample_patches(
    [(-79.30, -1.10)], patch_size=9, years=[2024, 2025]
)
# patches.shape == (1, 2, 64, 9, 9)

catalog = AEFCatalog("outputs/catalog.parquet")
values = catalog.sample_points([(-79.30, -1.10), (-80.10, 0.20)], years=[2025])

tile = catalog.open_tile("17MPU")
window = tile.read_window(0, 1024, 0, 1024, years=[2025])
# window.shape == (1, 64, 1024, 1024)

pieces = catalog.read_bbox(
    (-79.4, -1.2, -79.2, -1.0), years=[2025], crs="EPSG:4326"
)
# {grid_id: (values, affine_transform, crs), ...}
```

`AEFZarr.sample_points()` returns `[N,T,64]`, groups points by touched 64 x 64
chunk, and reads each touched chunk once. Points outside the store return NaN.
`AEFZarr.sample_patches()` returns centred odd-width patches as `[N,T,64,P,P]`;
parts beyond a tile edge are NaN-padded. Read patches in bounded batches rather
than materializing every training patch at once.
`AEFCatalog` routes points across multiple tile stores; pass explicit `tile_ids`
when points lie in overlapping tile margins. For lazy regional processing:

`open_tile()` works with reference IDs, Tessera grid names and MGRS tile IDs.
`read_window()` reads one pixel window without loading the full cube.
`read_bbox()` returns one native-grid piece per intersecting store and deliberately
does not silently reproject or mosaic grids from different UTM zones. For lazy
regional processing:

```python
ds = tile.open_xarray()
print(ds.embeddings.dims)  # ('time', 'band', 'y', 'x')
```

For a reproducible PyTorch delivery, extract bounded NPY shards plus matching
Parquet metadata:

```bash
python scripts/extract_aef_patches.py --samples data/model_points.parquet --catalog outputs/mgrs/catalog.parquet --out-dir outputs/patches_9x9 --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --patch-size 9 --batch-size 256 --grid-id-column mgrs_tile
```

Each NPY shard is `[N,T,64,P,P]` float32 and can be opened with
`numpy.load(path, mmap_mode="r")`, then passed to `torch.from_numpy`. Its metadata
shard retains labels, split, polygon ID and `patch_index`, and records patch
completeness and valid fraction. `--validate-only` reports the shape and
uncompressed storage estimate without reading tensors.

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

Use full MGRS Zarr tiles for spatial inference, point Parquet for pixel/centre
training and validation, and `sample_patches()` for 3 x 3, 5 x 5 or 9 x 9 model
inputs. This avoids maintaining duplicate patch files while preserving repeatable
access to the exact source cube.

The S1-GRiTS `utm_wkt` bounds for 17MPU produce a `10980 x 10980` grid at 10 m.
That is approximately 30.86 GB uncompressed for one year and 277.77 GB for nine
years. The multi-tile pilot's 17.9%-19.8% ratios imply roughly 50-55 GB for this
specific nine-year tile if its full data compress similarly, but only a complete
run can establish the final size. A national 11-tile dense download therefore
still requires an explicit storage decision; sparse point Parquet remains the
preferred training route when only a small fraction of pixels is needed.

### Chunk-size benchmark

Use `scripts/benchmark_zarr_chunks.py` to compare resolver behavior on bit-exact
copies of real stores. A four-tile, three-year warm-cache test found that 64 x 64
inner chunks reduced median 256 x 256 dense-window latency from 86.4 ms to 62.3 ms,
but increased single-point latency from 3.21 ms to 9.46 ms, 9 x 9 patch latency
from 3.80 ms to 6.62 ms, and storage by 1.9%. Therefore 64 x 64 is an
inference-oriented choice, not a universal storage improvement. Keep point
training in Parquet and materialize bounded patch shards if selecting 64 x 64 for
the complete inference cubes.

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
4. **Full-tile single-year test**: stream the 10 m 17MPU grid for 2025 using the fastest stable
   configuration. Verify its CRS, affine transform, dimensions, 64 bands, valid
   mask and disk size.
5. **Resume test**: interrupt a grid run after several blocks, rerun the identical
   command, and confirm completed blocks are reused and the final sampled values
   match a clean run.
6. **Full 17MPU nine-year test**: run 2017-2025 only after steps 1-5 pass. Use its
   observed time and storage, rather than a linear microbenchmark estimate, to
   approve or reject the 11-tile production run.

Before any full tile, run one bounded live probe under both grid schemes:

```bash
python scripts/smoke_test_aef_tiles.py --project YOUR_GEE_PROJECT --out-dir outputs/tile_smoke --year 2025 --probe-size 256 --tessera-tile -79.95 -1.05 --mgrs-index D:/Project/claude-demo/S1-GRiTS/src/s1grits/data/mgrs.parquet --mgrs-tile 17MPU
```

This uses the real Tessera 0.1-degree and MGRS definitions but downloads only the
central 256 x 256 block from each. It verifies CRS, transform, grid identity,
64-band completeness, catalog registration and the default lossless codec without
mistaking a smoke test for authorization to download an entire MGRS tile.

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
