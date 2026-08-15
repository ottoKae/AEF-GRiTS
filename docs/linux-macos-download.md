# Linux and macOS download environment

This guide installs only the packages required to stream AEF points and dense
grids directly from Earth Engine to local Parquet/Zarr. It supports Linux
x86-64 and macOS on both Intel and Apple Silicon.

## Recommended installation: Miniforge/conda-forge

Conda is preferred because GDAL, PROJ, GEOS, Rasterio and MGRS contain native
libraries. A single architecture-neutral environment specification is provided;
conda-forge resolves the correct binaries for `linux-64`, `osx-64`, or
`osx-arm64`.

```bash
git clone https://github.com/ottoKae/AEF-GRiTS.git
cd AEF-GRiTS
conda env create -f environment-download.yml
conda activate aef_grits_download
```

If `conda activate` is unavailable in a newly opened Unix shell, initialize the
shell once with `conda init`, reopen the terminal, or load it for the current
session with `source "$(conda info --base)/etc/profile.d/conda.sh"`.

Existing environments can be synchronized with:

```bash
conda env update -n aef_grits_download -f environment-download.yml --prune
conda activate aef_grits_download
```

Alternatively, after installing Miniforge, the helper performs the same create
or update operation:

```bash
bash scripts/bootstrap_unix.sh YOUR_GEE_PROJECT
conda activate aef_grits_download
```

Do not mix Homebrew/system GDAL with conda Rasterio in the same environment.
The conda environment already contains a matching native stack. A pure-pip
installation is available when compatible wheels exist, but is the fallback:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[download]"
```

If a restricted network already has the build tools and packages cached but
pip cannot create its temporary build-isolation environment, use
`python -m pip install -e ".[download]" --no-build-isolation`. Do not use this
option to bypass genuinely missing dependencies.

## Earth Engine login

For a Linux desktop or macOS computer with a local browser:

```bash
earthengine authenticate --auth_mode=localhost
earthengine set_project YOUR_GEE_PROJECT
```

For a remote/headless Linux machine, use the official gcloud flow when gcloud
is installed, or notebook authentication when a local callback is impossible:

```bash
earthengine authenticate --auth_mode=gcloud
# or
earthengine authenticate --auth_mode=notebook
earthengine set_project YOUR_GEE_PROJECT
```

Credentials normally persist at
`~/.config/earthengine/credentials`. Never copy that file into this repository.
For unattended institutional infrastructure, use an authorized service account
and `GOOGLE_APPLICATION_CREDENTIALS` rather than sharing a personal token.

Official references:

- <https://developers.google.com/earth-engine/guides/python_install-conda>
- <https://developers.google.com/earth-engine/guides/auth>
- <https://developers.google.com/earth-engine/guides/command_line>

## Validate before downloading

The doctor checks Python and package versions, output writability/free space,
PROJ transformation, the packaged global MGRS index, a lossless Zarr v3
Zstd-7/no-shuffle round trip, saved credentials, and real Earth Engine
initialization:

```bash
aef-grits-doctor \
  --project YOUR_GEE_PROJECT \
  --output /data/aef \
  --minimum-free-gib 20
```

For an offline installation check, add `--skip-ee`. Use `--json` for machine
readable CI/HPC output. A failed required check returns a non-zero exit code.
If a console command is not yet visible on `PATH`, the equivalent module calls
are `python -m aef_grits.doctor`, `python -m scripts.stream_aef_points_ee`, and
`python -m scripts.stream_aef_grid_ee`.

## Point download

First validate without contacting Earth Engine:

```bash
aef-grits-points \
  --samples /data/points.csv \
  --out-dir /data/aef/points_2025 \
  --years 2025 \
  --validate-only
```

Then run the resumable download:

```bash
aef-grits-points \
  --samples /data/points.csv \
  --out-dir /data/aef/points_2025 \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --chunk-size 1000 \
  --workers 1
```

CSV requires unique `sample_id` and WGS84 `lon,lat`. Shapefile, GeoPackage and
GeoJSON are also supported by the CLI and are converted according to the
explicit `--geometry-mode`. Results are atomic Parquet shards plus
`catalog.parquet` and `report.json`. Re-running the identical command resumes
completed shards.

`--out-dir` is authoritative. If omitted, the console command writes to
`$AEF_GRITS_OUTPUT_ROOT/point_stream`, or `./outputs/point_stream` when the
environment variable is absent.

## MGRS grid download

Always inspect a grid plan before the real download:

```bash
aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 17MPU \
  --out-dir /data/aef/mgrs \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --plan-only
```

The plan reports the exact packaged MGRS definition, UTM CRS, dimensions,
uncompressed float32 size, output path, and current free space. If acceptable,
remove `--plan-only`:

```bash
aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 17MPU \
  --out-dir /data/aef/mgrs \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --workers 2
```

## Tessera 0.1-degree download

Coordinates are the 0.1-degree cell centre:

```bash
aef-grits-grid \
  --grid-scheme tessera_0p1 \
  --tessera-tile -80.05 -1.05 \
  --out-dir /data/aef/tessera \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --plan-only
```

Remove `--plan-only` after checking the plan. For all grid modes the production
storage contract remains float32, `chunks=(1,64,64,64)`,
`shards=(1,64,512,512)`, lossless Zstd-7 and no shuffle. Re-running the same
command uses its progress ledger and resumes completed blocks.

## Long-running jobs

On Linux, run within `tmux` or a batch scheduler so an SSH disconnect does not
terminate Python:

```bash
tmux new -s aef
conda activate aef_grits_download
aef-grits-grid ... 2>&1 | tee /data/aef/download.log
```

On a Mac laptop, prevent system sleep for the command duration:

```bash
caffeinate -i aef-grits-grid ... 2>&1 | tee /Volumes/AEF/download.log
```

Prefer a local APFS, ext4 or XFS volume. Confirm that external macOS disks are
not FAT32 (4 GiB file limit). Network filesystems may have slower metadata and
atomic-rename behavior; test a Tessera tile before committing to a large MGRS
run. Keep at least the uncompressed plan estimate plus operational reserve when
possible, even though the lossless Zarr product is normally smaller.

Linux NTFS/NTFS3 must be treated as a completed-product destination, not an
active Zarr work volume. Supply `--state-dir` and `--staging-dir` on ext4/XFS;
the downloader enforces this policy. PID files and shell redirection logs must
also be placed under the native state directory. Full recovery instructions
are in [NTFS-safe operation and recovery](ntfs-safe-download.md).

## Updating and reproducing

```bash
git pull --ff-only
conda env update -n aef_grits_download -f environment-download.yml --prune
conda activate aef_grits_download
aef-grits-doctor --project YOUR_GEE_PROJECT --output /data/aef
```

Record `git rev-parse HEAD`, `conda list --explicit`, the command line,
`catalog.parquet`, report JSON and progress ledger with each production run.
