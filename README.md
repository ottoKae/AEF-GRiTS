# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

**Platforms:** Windows · macOS (Intel and Apple Silicon) · Linux<br>
**Interfaces:** local Web application · Python command line

AEF-GRiTS downloads annual 64-dimensional AlphaEarth Foundation (AEF)
embeddings directly from Google Earth Engine to local storage. It supports
sparse training samples and dense 10 m mapping grids without staging files in
Google Drive.

Data source: `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`, bands `A00`–`A63`.

## What it provides

- Point sampling from CSV, Shapefile, GeoPackage, GeoJSON, or Parquet.
- Dense 10 m Zarr cubes on Tessera 0.1°, MGRS, or a reference raster grid.
- A local Web interface for selecting inputs, years, AOIs, and output folders.
- Resumable downloads, atomic metadata, disk/size checks, and output validation.
- Readers for points, windows, bounding boxes, and model-ready patches.
- A packaged global MGRS index; no other source repository is required.

## Quick start

Install Miniforge or Conda, clone the repository, and run:

```bash
conda env create -f environment-download.yml
conda activate aef_grits_download
earthengine authenticate --auth_mode=localhost
earthengine set_project YOUR_GEE_PROJECT
aef-grits-doctor --project YOUR_GEE_PROJECT --output ./outputs
```

The same environment file works on Windows, macOS, and Linux. On Windows, run
the commands in Anaconda Prompt or PowerShell. For remote Linux and macOS
details, see [Linux and macOS setup](docs/linux-macos-download.md).

## Web application

Start the local server from the repository root:

```bash
conda activate aef_grits_download
python webapp/app.py
```

Open `http://127.0.0.1:5555`, then:

1. Choose **Points** or **Grid**.
2. Select a point file, or choose MGRS/Tessera and define an AOI by Shapefile
   or vector drawing.
3. Select years and a server-side output folder, then press **Download**.

The server performs preflight checks and asks for confirmation before starting.
Tasks remain visible in the horizontal task dock with progress, current-grid
ETA, output path, validation status, logs, and reproducibility metadata.

The default output root is `webapp/output`. To use another location:

```bash
# Linux/macOS
export AEF_GRITS_WEB_OUTPUT=/data/aef
python webapp/app.py

# Windows PowerShell
$env:AEF_GRITS_WEB_OUTPUT = "D:\data\aef"
python webapp/app.py
```

The Web app listens only on localhost and is intended for one trusted local
user. Full details are in [webapp/README.md](webapp/README.md).

## Point download

CSV input must contain unique `sample_id`, `lon`, and `lat` columns in WGS84.
Vector inputs must declare a CRS; coordinates are converted to WGS84 before
Earth Engine sampling.

Validate without contacting Earth Engine:

```bash
aef-grits-points \
  --samples samples.csv \
  --years 2024 2025 \
  --validate-only
```

Download annual embeddings to atomic Parquet shards:

```bash
aef-grits-points \
  --samples samples.csv \
  --project YOUR_GEE_PROJECT \
  --years 2024 2025 \
  --out-dir outputs/points
```

For Shapefiles, GeoPackages, GeoJSON, and polygon-to-point conversion, use the
same command and select `--geometry-mode`. Run `aef-grits-points --help` for all
options.

## Dense grid download

All dense products use 10 m local UTM grids and the lossless storage contract:

- Zarr v3, float32
- chunks `(1,64,64,64)`
- shards `(1,64,512,512)`
- Zstd level 7, no shuffle

Plan a Tessera 0.1° tile without downloading:

```bash
aef-grits-grid \
  --grid-scheme tessera_0p1 \
  --tessera-tile -79.95 -1.05 \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --out-dir outputs/tessera \
  --plan-only
```

Remove `--plan-only` to download. For MGRS:

```bash
aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 17MPU \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --out-dir outputs/mgrs \
  --plan-only
```

An arbitrary 10 m GeoTIFF can define a `reference` grid. Run
`aef-grits-grid --help` for all grid modes and tuning options.

### Linux NTFS delivery targets

Do not run a Zarr download directly on a Linux `ntfs3` mount. Use native ext4
or XFS directories for active state and one-grid staging, and use NTFS only as
the completed-product destination:

```bash
aef-grits-grid \
  --grid-scheme mgrs --tiles 50RMU \
  --project YOUR_GEE_PROJECT --years 2019 \
  --out-dir /mnt/hdda/user/anhui_mgrs_2019 \
  --state-dir /home/user/aef_state/anhui_mgrs_2019 \
  --staging-dir /home/user/aef_staging/anhui_mgrs_2019
```

On Linux, the command detects the output filesystem before touching it. An
NTFS/NTFS3 final path is rejected unless both control state and staging are on
a non-NTFS mount. Downloads write one block at a time to staging; an isolated
single-writer process then delivers one completed store to NTFS. A copy timeout
records an incident on the state volume and performs no automatic deletion.
See [NTFS-safe operation and recovery](docs/ntfs-safe-download.md) or the
[Chinese guide](docs/ntfs-safe-download.zh-CN.md).

### Memory-bounded concurrency

Grid and point commands accept `--workers auto` and detect the smaller of host
available memory, Linux cgroup headroom, and an optional
`--memory-limit-gib`. Requests use a bounded in-flight queue; the point workflow
does not materialize every chunk future. Multiple CLI/Web jobs share a
cross-process Earth Engine request-token pool, while completed products share
one exclusive final-delivery lock.

For independently launched jobs, configure one native shared resource root:

```bash
export AEF_GRITS_RESOURCE_STATE=/home/user/aef_state/.resource_locks
```

Plans and reports record the resolved workers, estimated peak memory, actual
peak RSS, and throttle count. See
[resource-bounded downloads](docs/resource-bounded-download.md).

## Resolve grid IDs from an AOI

Convert a Shapefile, GeoPackage, GeoJSON, or GeoParquet AOI into deterministic
MGRS and Tessera grid lists:

```bash
python scripts/resolve_aef_grid_ids.py \
  --aoi province.shp \
  --schemes mgrs tessera_0p1 \
  --out-dir outputs/grid_lookup
```

The bundled `aef_grits/data/mgrs.parquet` is the authoritative MGRS geometry.

## Output structure

| Workflow | Main output | Supporting files |
|---|---|---|
| Points | Parquet shards | `catalog.parquet`, validation/report JSON |
| Dense grid | one Zarr per grid ID | `catalog.parquet`, progress/report JSON |
| Web task | the same products | task log, events, validation and provenance |

Downloaded data, credentials, logs, task state, and local outputs are excluded
from Git.

## Read local Zarr data

```python
from aef_grits.store import AEFZarr

cube = AEFZarr("outputs/mgrs/17MPU/aef_17MPU_2025_2025.zarr")
vector = cube.sample_at(x, y, year=2025, crs="EPSG:32717")
patches = cube.sample_patches(
    [(x, y)], years=[2025], patch_size=9, crs="EPSG:32717"
)
window = cube.read_window(0, 256, 0, 256, years=[2025])
```

Zarr reads are decompressed automatically through the standard Zarr API.

## Reliability

- Point shards and metadata are committed atomically.
- Dense grids save block checkpoints and resume compatible outputs.
- Linux NTFS is final-delivery-only; checkpoints, catalogs, reports, PID files,
  logs, and active Zarr writes remain on a native filesystem.
- Existing outputs are protected by request signatures.
- The Web app uses signed one-use plans, bounded queues, process-tree control,
  disk limits, and automatic product validation.
- CRS, transform, years, bands, compression, and source metadata are stored with
  every grid product.

## Repository guide

```text
aef_grits/       core grids, point conversion, readers, resources, doctor
scripts/         point/grid downloaders and preparation utilities
webapp/          localhost Web interface and task runner
tests/           unit, API, storage, CRS, and browser tests
docs/            data layout and operational documentation
```

Key documentation:

- [Data layout](docs/data-layout.md)
- [Direct Earth Engine streaming](docs/direct-streaming.md)
- [Linux and macOS download environment](docs/linux-macos-download.md)
- [Web application](webapp/README.md)

## Test

```bash
python -m pytest -q
```

Browser tests use Playwright when Chrome/Chromium is available. Large Earth
Engine downloads are not started by the automated test suite.
