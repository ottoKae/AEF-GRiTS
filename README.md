# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

> **GUI development branch:** `feature/web-workbench` keeps the unfinished
> homepage and browser-workbench updates separate from `main`. It already uses
> the native-UTM download core; this branch is not a finished website release.

AEF-GRiTS downloads annual 64-dimensional AlphaEarth Foundation embeddings
directly from Google Earth Engine to local storage. It supports sparse point
samples and dense 10 m grids without staging products in Google Drive.

**Platforms:** Windows, macOS (Intel/Apple Silicon), Linux

**Interfaces:** local Python engine + browser GUI (recommended), command line,
and optional shared OAuth Web deployment.

Dataset: `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`, bands `A00`–`A63`, years
2017–2025.

## Features

- CSV, Parquet, Shapefile, GeoPackage, and GeoJSON point inputs.
- Tessera 0.1°, MGRS, and reference-raster dense grids.
- 10 m float32 Zarr v3 with lossless Zstd-7/no-shuffle compression.
- Choose Zarr, Zarr + annual 64-band GeoTIFF, or GeoTIFF-only delivery.
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

Or, from a repository checkout or extracted source ZIP, install into an existing
Python environment (3.11/3.12 are the CI targets):

```bash
python -m pip install ".[download,web]"
aef-grits-web
```

The launcher opens `http://127.0.0.1:5555/portal`. If the executable is not on
PATH, use `python -m webapp.launcher` in the same environment. No public website
or PyPI release is implied by these source-install instructions.

## Agent-assisted local and SSH downloads

The repository includes the [aef-grits-download skill](.agents/skills/aef-grits-download/SKILL.md)
for planning, existing-data checks, local/SSH execution, monitoring, resume and
delivery verification. Start a skill-aware agent in this checkout. If discovery
is unavailable, ask it to read that file explicitly before operating the CLI.
No other GRiTS repository or global skill copy is required.

Example requests:

> Use $aef-grits-download to inspect my samples.csv and plan 2025 AEF extraction
> into my selected data directory. Offline preflight only; do not log in or
> download. Report sample counts, capacity and missing prerequisites.

> Use $aef-grits-download on my specified SSH alias, checkout and Python environment
> for a 2025 MGRS 50RMU job. Check existing data, memory and storage, execute within
> the agreed budget, preserve checkpoints and verify actual output. Report the
> server paths; do not transfer data back yet.

Specify execution host, years, points/AOI/grid IDs, format and storage roots;
remote work also needs the SSH identity, interpreter and resource budget.
Credentials belong to the execution user, not the public website. The skill
operates within user authorization; it neither grants automatic login/deletion
nor implements an unlimited scheduler. Remote production and local delivery are
separate steps.

The skill is distributed in the source checkout/archive, not installed by the
Python wheel. Offline `--plan-only` / `--validate-only` still write small local
planning/state files. See [command and validation details](.agents/skills/aef-grits-download/references/commands.md)
and [SSH operations](.agents/skills/aef-grits-download/references/ssh.md).

## Authenticate explicitly

Downloads never open a browser or start authentication. Log in once through
the dedicated command, then verify the selected account and Project:

```bash
aef-grits-auth login \
  --source earthengine \
  --auth-mode localhost \
  --project YOUR_GEE_PROJECT

aef-grits-auth verify --source auto --project YOUR_GEE_PROJECT --check-aef
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

### Native pixel alignment

New downloads use `native-utm-v1`. WGS84 `lon,lat` locate a point; its feature
vector comes from the containing **native UTM 10 m pixel**, not a resampled
longitude/latitude mosaic. Each year records the source asset, CRS, pixel
row/column and centre. A shard's `.native.json` freezes asset versions and grids.
Points prefer their local UTM zone; a valid overlapping source is a fallback only
in its own native grid, recorded in the output CRS field.

Dense downloads accept only sources sharing the target CRS and pixel boundaries.
Cross-UTM filling and shifted reference grids are rejected; missing pixels remain
NaN. A north-up row reversal is lossless. MGRS and 0.1° are storage/selection
units, not a guarantee that another sensor has the same grid.

Offline plans do not verify remote alignment. Downloads verify it before writing;
Zarr, TIFF and catalogs retain source manifests. Old products/checkpoints cannot
be resumed under the new protocol: use a **new output directory**. Completed
TIFF recovery checks its frozen manifest offline rather than claiming the remote
source is unchanged. The legacy Drive point exporter is plan-only; use the direct
point command above. Alignment preserves pixel identity at the requested float32
precision; it does not claim bit-identical source double encoding.

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

For GIS use, choose **Zarr + GeoTIFF** (`--export-geotiff`) or **GeoTIFF only**
(`--geotiff-only`); the flags are mutually exclusive. Each year becomes a
64-band float32 TIFF with lossless DEFLATE, CRS, transform, band names and NaN
NoData. Large rasters use BigTIFF (explicitly at 4 GiB uncompressed or above).

GeoTIFF-only uses a task-owned temporary Zarr cache for resumable downloading.
It removes that cache only after successful TIFF validation, leaving TIFFs,
catalog/manifest and reports. Failure retains the cache for resume; existing
Zarr products are never deleted. Peak disk space must still cover both cache and
TIFFs. Zarr + GeoTIFF keeps both products. Export is windowed and currently
rejected for Linux NTFS/NTFS3 destinations. Point products remain Parquet.

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

For one trusted local user, after installation:

```bash
aef-grits-web
# Optional: choose another disk or suppress automatic browser opening
aef-grits-web --output-root /your/data/aef --no-browser
```

The four steps are **P0 connection → P1 points/grid → P2 years/query → P3 local
output/download**. In P0, explicitly detect credentials, authorize locally when
needed, and verify your Project and AEF access. Finding a credential is not proof
of access. The four preparation checks contribute 25% each; that is not download
progress. Preflight and confirmation still precede execution.

The new workspace at `http://127.0.0.1:5555/portal` adds product selection,
planning results, plan export, map navigation and a tabular download manager.
The original compact view remains at `/`. Only AEF is enabled; the Tessera 0.1°
grid is a storage layout, not the Tessera embedding product. The supported GUI
download route is GEE. The anonymous-source option is an experimental connectivity
probe/library, **not yet an end-to-end GUI downloader**.

Python writes directly to the local filesystem; no browser file-save API or ZIP
transfer is needed. The default data root is `~/Downloads/AEF-GRiTS`. State/logs
use the user's application state directory, not the installed package. Closing
a tab does not stop Python; exiting the process or sleeping the computer may
interrupt work. Restart and resume explicitly.

In local mode, the folder tile opens the operating system's folder chooser.
You may select another disk; the task gets its own automatically named directory
inside the chosen folder. Cancel leaves the previous selection unchanged. The
selected disk is checked during preflight and its path is bound to the plan.
Use the separate Web folder browser when Tk or a desktop session is unavailable;
it stays inside the configured output root. Native dialogs require optional Tk
in the Python environment (for Debian/Ubuntu system Python, the `python3-tk`
system package, not a pip package). Remote deployments cannot open a chooser on
the visitor's computer: native dialogs belong to the machine running Python.

Existing `webapp/runs` and `webapp/output` are not moved automatically. To reuse
them, pass their absolute paths with `--state-dir` and `--output-root` after
stopping the old instance. Changing the launcher does not restart a running GUI.
See the [illustrated workspace guide (Chinese)](docs/web-portal.zh-CN.md).

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
automatically. GeoTIFF-only results instead open directly in QGIS or Rasterio;
they do not require the removed temporary Zarr cache. Their catalog is not a
Zarr catalog and must not be passed to `AEFZarr` or `AEFResolver`.

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
.agents/skills/  repository-owned agent download procedures
aef_grits/       authentication, grids, readers, storage and resource controls
scripts/         point/grid downloaders and preparation utilities
webapp/          local/shared Web UI and reliable task runner
tests/           unit, API, CRS, storage and browser tests
docs/            operation, data layout and deployment guides
```

Additional references: [data layout](docs/data-layout.md),
[direct streaming](docs/direct-streaming.md), and
[Linux/macOS setup](docs/linux-macos-download.md).
