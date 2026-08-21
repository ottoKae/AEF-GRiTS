# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

AEF-GRiTS downloads annual 64-dimensional AlphaEarth Foundation embeddings
directly from Google Earth Engine to local storage. It supports sparse point
samples and dense 10 m grids without staging products in Google Drive.

**Platforms:** Windows, macOS (Intel/Apple Silicon), Linux

**Interfaces:** command line, local Web application, shared OAuth Web deployment

Dataset: `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`, bands `A00`–`A63`, years
2017–2025.

## Features

- CSV, Parquet, Shapefile, GeoPackage, and GeoJSON point inputs.
- Tessera 0.1°, MGRS, and reference-raster dense grids.
- 10 m float32 Zarr v3 with lossless Zstd-7/no-shuffle compression.
- Bounded memory/concurrency, request deadlines, classified retry, checkpoints,
  atomic metadata, storage guards, and product validation.
- Point/patch/window readers for model training and inference.
- Packaged authoritative MGRS geometry; no other repository is required.
- Existing-credential discovery and optional per-user Web OAuth.

## Install

```bash
conda env create -f environment-download.yml
conda activate aef_grits_download
```

Or install into an existing Python 3.10+ environment:

```bash
python -m pip install -e ".[download,web]"
```

## Authenticate explicitly

Downloads never open a browser or start authentication. Log in once through
the dedicated command, then verify the selected account and Project:

```bash
aef-grits-auth login \
  --source earthengine \
  --auth-mode localhost \
  --project YOUR_GEE_PROJECT

aef-grits-auth verify --source auto --project YOUR_GEE_PROJECT
aef-grits-doctor --auth-source auto --project YOUR_GEE_PROJECT --output ./outputs
```

`auto` reuses an explicit ADC configuration, otherwise the current user's
Earth Engine credential, otherwise local/cloud ADC. An invalid explicit source
fails closed and never switches identity. The Project can be supplied by
`--project` or private local credential configuration; no real Project is
embedded in this repository.

See [Authentication and identity boundaries](docs/authentication.md) for
personal computers, SSH servers, campus Web deployments, service accounts,
Google Cloud, and CI.

## Point download

CSV requires unique `sample_id`, `lon`, and `lat` columns in WGS84. Vector
inputs must declare a CRS and are transformed to WGS84 before Earth Engine
sampling.

```bash
# Offline input validation
aef-grits-points --samples samples.csv --years 2024 2025 --validate-only

# Download with credentials already configured
aef-grits-points \
  --samples samples.csv \
  --project YOUR_GEE_PROJECT \
  --years 2024 2025 \
  --out-dir outputs/points
```

Results are atomic Parquet shards with `catalog.parquet` and validation/report
JSON. Large sources are preflighted and downloaded in bounded batches.

## Dense grid download

```bash
# Plan only; no Earth Engine call
aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 17MPU \
  --years 2025 \
  --out-dir outputs/mgrs \
  --plan-only

# Remove --plan-only and provide/configure a Project to download
aef-grits-grid \
  --grid-scheme tessera_0p1 \
  --tessera-tile -79.95 -1.05 \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --out-dir outputs/tessera
```

Each grid ID becomes one Zarr store with shape
`[year, 64, y, x]`, chunks `(1,64,64,64)`, shards `(1,64,512,512)`, float32,
Zstd level 7, and no shuffle.

For Linux NTFS/NTFS3 destinations, active state and staging must remain on
ext4/XFS; NTFS receives only completed products:

```bash
aef-grits-grid \
  --grid-scheme mgrs --tiles 50RMU --years 2019 \
  --project YOUR_GEE_PROJECT \
  --out-dir /mnt/hdda/user/aef \
  --state-dir /home/user/aef_state \
  --staging-dir /home/user/aef_staging
```

See [NTFS-safe operation](docs/ntfs-safe-download.md) and
[resource-bounded downloads](docs/resource-bounded-download.md).

## Web application

For one trusted local user:

```bash
python webapp/app.py
```

Open `http://127.0.0.1:5555`, enter your Earth Engine Project, choose Points or
Grid, select years/output, and start the two-stage preflight/confirmation flow.
The Web runner uses the same tested CLI downloaders.

For a campus/shared server, enable per-user OAuth. Do not configure a global
Project fallback; every user logs in and selects a Project they can use. Tasks,
credentials, and outputs are isolated by anonymous owner ID. Deployment steps
are in [Authentication](docs/authentication.md) and [Web application](webapp/README.md).

## Read results

```python
from aef_grits import open_aef_point_dataset
from aef_grits.store import AEFZarr

points = open_aef_point_dataset("outputs/points", years=[2025])
for batch in points.iter_batches(years=[2025]):
    consume(batch)

cube = AEFZarr("outputs/mgrs/17MPU/aef_17MPU_2025_2025.zarr")
vector = cube.sample_at(x, y, year=2025, crs="EPSG:32717")
patches = cube.sample_patches([(x, y)], years=[2025], patch_size=9, crs="EPSG:32717")
window = cube.read_window(0, 256, 0, 256, years=[2025])
```

The standard Zarr API detects compression metadata and decompresses reads
automatically.

## AOI to grid IDs

```bash
python scripts/resolve_aef_grid_ids.py \
  --aoi province.shp \
  --schemes mgrs tessera_0p1 \
  --out-dir outputs/grid_lookup
```

## Test

```bash
python -m pytest -q
```

CI runs Python 3.11/3.12 on Windows, macOS, and Linux. Browser tests run with
Playwright. Automated tests use synthetic data and do not start large Earth
Engine downloads.

## Repository layout

```text
aef_grits/       authentication, grids, readers, storage and resource controls
scripts/         point/grid downloaders and preparation utilities
webapp/          local/shared Web UI and reliable task runner
tests/           unit, API, CRS, storage and browser tests
docs/            operation, data layout and deployment guides
```

Additional references: [data layout](docs/data-layout.md),
[direct streaming](docs/direct-streaming.md), and
[Linux/macOS setup](docs/linux-macos-download.md).
