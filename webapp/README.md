# AEF-GRiTS Web application

> **Testing preview:** both the AEF download workflow and Web GUI downloads are
> still under testing. Validate a small output before scaling up; this is not a
> production-ready release.

The application plans and runs the existing AEF-GRiTS point and dense-grid
streaming commands. It supports a trusted localhost mode and an optional
per-user OAuth mode for a campus server. It does not maintain a second download
implementation: every accepted plan becomes the tested command-line interface.

## Install, test, and start

From a repository checkout or extracted source ZIP, install once:

```powershell
python -m pip install ".[download,web]"
aef-grits-web
```

The installed launcher opens `http://127.0.0.1:5555/portal`; it also works outside
the repository. `python -m webapp.launcher` is the module fallback. Options:
`--port`, `--no-browser`, `--output-root`, `--state-dir`. No public listener is
enabled. The old source entry `python webapp/app.py` remains available.

Default data goes to `~/Downloads/AEF-GRiTS`. State/log defaults are
`%LOCALAPPDATA%/AEF-GRiTS` on Windows, `~/Library/Application Support/AEF-GRiTS`
on macOS, and `$XDG_STATE_HOME/aef-grits` or `~/.local/state/aef-grits` on Linux.
Explicit command arguments override the corresponding environment variables.

Existing source-tree runs/output are not relocated. Stop the old instance and
pass their absolute paths with `--state-dir` and `--output-root` to reuse them.
Do not point a new instance at a running instance's state directory. Closing a
browser tab does not stop Python; exiting the process may interrupt tasks, which
can be resumed explicitly after restart.

### Two views, one backend

- `/`: the compact local interface.
- `/portal`: product selection, planning table, plan export and download manager.

Use the header link to switch views. Both views reuse the same authentication,
preflight, signed plans, queue and CLI downloaders; this is not a second service.
Only **AEF** is enabled. The disabled Tessera product is not downloadable; the
Tessera 0.1° AEF storage grid remains available.

P0 highlights the next connection action; successful verification collapses the
settings. Changing Project invalidates access readiness. The anonymous source
probe is under experimental sources and cannot start a download. For a first
task, download the four-point CSV linked in P1, upload it and select 2025.
Inspect Results and copy the output Path after the task finishes; a preparation
percentage or successful preflight does not mean data has been downloaded.

In `/portal`, press **检索 / 预检**, inspect the resolved grid IDs or point file,
years, CRS and size estimate, then press **确认下载**. Planning alone creates no
download task. Editing the form invalidates the prepared plan. The planner
checks local geometry/capacity, **not remote GEE imagery availability**.
**导出计划** exports a JSON summary without the signed authorization token or
Project ID; it is not an executable task manifest.

The download manager filters queued, active, completed and problem tasks and
shows output paths, validation, logs, progress, actual block processing rate
and current-grid ETA when supplied by the worker. Block rate is not MB/s.
Credentials requiring renewal can be explicitly reauthorized and resumed.
Results are written directly by Python. In the recommended local mode this is
the user's own disk; no archive download or browser filesystem permission is
needed. In optional remote deployments it remains the server's disk.

The supported GUI download source is GEE. Anonymous AEF COG access is currently
an experimental connectivity probe and Python reader, not an enabled end-to-end
GUI download route. A successful probe must not be interpreted as a download.

Screenshots and local usage: [Chinese guide](../docs/web-portal.zh-CN.md).

Run the API/unit suite and browser workflow tests with isolated disposable state:

```powershell
python -m pip install -e ".[gee,geo,stream,web,web-e2e]"
$run = Join-Path $env:TEMP ("aef-web-tests-" + [guid]::NewGuid().ToString('N'))
$env:AEF_GRITS_WEB_STATE = Join-Path $run 'state'
$env:AEF_GRITS_WEB_OUTPUT = Join-Path $run 'output'
$env:AEF_GRITS_RESOURCE_STATE = Join-Path $run 'resources'
New-Item -ItemType Directory -Path $run -Force | Out-Null
python -m pytest tests/test_webapp.py tests/test_webapp_e2e.py tests/test_web_auth.py -p no:cacheprovider --basetemp "$run/pytest"
```

Run these commands in a dedicated test terminal; do not reuse its temporary
output/state environment to start production downloads.

The Playwright tests reuse an installed Chromium/Chrome executable when one is
available. They use synthetic command execution; they do not download Earth
Engine data.

## Two-stage protocol

Both views share P0–P3:

1. **P0 connection:** detect credentials, explicitly authorize locally if needed,
   then verify the Earth Engine **Project ID** and AEF access; never enter a password.
2. **P1 location:** choose Points/Grid; upload CSV/point Shapefile or click map
   points; for grids, set an AOI and select intersecting MGRS/Tessera cells.
3. **P2 years/query:** choose years and inspect the local preflight result.
4. **P3 storage/download:** choose a local folder and raster format, then confirm.

Preparation has four checks at 25% each: connection, spatial input, years and
configured storage. 100% means the form is ready, not that data has downloaded
or disk checks have passed. The actual task progress is displayed separately.

Map points become a WGS84 CSV consumed by the existing point workflow.
Grid selections are revalidated against the AOI, reflected in the size estimate
and bound into a new signed plan. Entire selected cells are downloaded, not
only the pixels inside the input polygon. No selected cell means no download.

## Authentication modes

`AEF_GRITS_WEB_AUTH_MODE=local` is the default. It is suitable for one trusted
local user and reuses credentials already configured for the service account or
OS user. No Project is hard-coded in the frontend or repository. The local
buttons invoke the existing authentication CLI in an isolated process:
**检测凭据**, **本机 Google 授权**, **验证 Project 与 AEF**, and **取消等待**.
Discovery and successful login alone are not proof of Project/collection access.
Local credentials stay on this computer and are not sent to a public website.
Login is explicit and blocked while download tasks are active; the download
worker never invokes it automatically. ADC login remains a deliberate CLI action.

For a shared server, use `AEF_GRITS_WEB_AUTH_MODE=oauth`. Each user explicitly
clicks **Login Earth Engine**, supplies a Project they can access, and completes
Project verification before a plan is signed. Plans and tasks are bound to an
anonymous owner hash, credential version, and Project fingerprint. Users can
only list, read, cancel, resume, or browse their own outputs.

```bash
aef-grits-auth vault-init \
  --database /var/lib/aef-grits/auth/vault.sqlite \
  --key-file /etc/aef-grits/vault.key

export AEF_GRITS_WEB_AUTH_MODE=oauth
export AEF_GRITS_WEB_OAUTH_CLIENT=/etc/aef-grits/oauth-client.json
export AEF_GRITS_WEB_OAUTH_REDIRECT_URI=https://aef.example.edu/api/auth/callback
export AEF_GRITS_AUTH_VAULT=/var/lib/aef-grits/auth/vault.sqlite
export AEF_GRITS_AUTH_VAULT_KEY=/etc/aef-grits/vault.key
export AEF_GRITS_WEB_ALLOWED_DOMAINS=example.edu
export AEF_GRITS_WEB_TRUSTED_HOSTS=aef.example.edu
export AEF_GRITS_WEB_PROXY_COUNT=1
```

Use HTTPS and keep the vault on ext4/XFS. Do not set a server-wide
`AEF_GRITS_PROJECT` in shared mode. Authorization failure preserves the
checkpoint and marks the task `auth_required`; the owner explicitly logs in
and resumes it. See [authentication boundaries](../docs/authentication.md).

Technical retry, chunk, checkpoint and CRS settings are not shown in the main
form. Safe tested defaults are used. Capacity planning still happens after the
single Download click and before the confirmation dialog.

In the compact view, the left sidebar is reserved for defining a new download. Task history is a
single horizontal dock below the map, forming an L-shaped workspace with the
sidebar. The newest task is always placed at the left. Arrow controls and native
horizontal scrolling expose older tasks without adding a second row. Each card
shows the download target, spatial scope or source file, years, output CRS,
storage path, product-validation status, and progress.

Native browser file inputs are visually replaced by consistent large picker
tiles. The CSV and Shapefile modes still use the browser's secure file chooser,
but the chosen names are reported inside the designed tile. Grid AOI import and
vector drawing use the same icon-first layout.

In local mode, output selection opens the operating system's folder chooser in
a short-lived Python/Tk child process. The user may choose another disk; a new
task directory is created inside the selection. Cancel preserves the old choice.
The selection grant is bound into the signed plan and capacity is checked on
the selected disk. Arbitrary absolute paths in browser requests are not accepted.

The separate Web folder browser remains confined to `AEF_GRITS_WEB_OUTPUT` and
is the fallback for missing Tk or headless operation. Remote/shared mode uses
this browser rather than a native dialog: a server cannot open the visitor's
local folder chooser. Tk is optional for downloading; Debian/Ubuntu system
Python uses the distribution's `python3-tk` package when native dialogs are
needed, not `pip install tkinter`. Other environments need their matching Tk.

All work follows **preflight -> explicit confirmation -> execution**:

1. Grid forms call `POST /api/plan`; point forms upload once to
   `POST /api/plan/points`.
2. The server validates the workflow, years, input/AOI, grid count, output path,
   uncompressed byte estimate, free disk, and configured limits. Point preflight
   verifies IDs/coordinates and point geometry, and reports rows, shards, 64
   feature columns, source CRS, and the normalized WGS84 CRS.
3. The response contains a signed, expiring, one-use `plan_id`. The browser
   displays its exact grid IDs and authoritative WGS84 footprints before asking
   for confirmation.
4. `POST /api/tasks` accepts only that `plan_id`. Plans above the warning
   threshold require the exact phrase shown by the server, for example
   `DOWNLOAD 57.49 GiB`. Refreshing or replaying a consumed plan cannot create a
   second task.

The estimate is based on uncompressed float32 values and is deliberately not a
compressed-storage promise. Both TIFF modes reserve extra raw space for Zarr
(permanent or temporary) and additional rasters during download/export.

## Raster format

Zarr is the default. **Zarr + GeoTIFF** (`--export-geotiff`) keeps both products;
**GeoTIFF only** (`--geotiff-only`) delivers TIFFs and catalog/manifest/reports.
The two CLI flags are mutually exclusive. Each year has one 64-band float32
TIFF with windowed lossless DEFLATE, CRS/transform/band names and NaN NoData;
large rasters use BigTIFF (explicitly at 4 GiB uncompressed or above).

GeoTIFF-only first downloads through a task-owned temporary Zarr cache. It
removes only that cache after successful TIFF validation; failure keeps it for
resume. Existing Zarr products are never deleted. Peak disk space still covers
both cache and TIFFs; this is not a cache-free direct-TIFF writer. Completed
TIFFs remain readable without Zarr, with their provenance in metadata.

TIFF export is rejected for Linux NTFS/NTFS3 final targets; choose a native
filesystem. Point downloads remain Parquet. Runtime validation checks TIFF
structure/provenance and recorded size, not every pixel; synthetic tests check
exported values and masks.

## Workflow and CRS contract

| Workflow | Selection | Local output |
|---|---|---|
| Points | CSV or Point/MultiPoint Shapefile | sharded Parquet |
| MGRS | exact AOI intersection with packaged `aef_grits/data/mgrs.parquet` | UTM Zarr and/or annual GeoTIFF per MGRS grid |
| Tessera 0.1 degree | positive-area AOI intersection with 0.1-degree cells | UTM Zarr and/or annual GeoTIFF per Tessera cell |

The browser exposes only MGRS and Tessera dense grids. Reference grids remain
available through the documented Python/CLI API for advanced reproducible work.

The browser/server exchange AOI geometry in `EPSG:4326`. Leaflet displays it in
Web Mercator (`EPSG:3857`) only for visualization. Server-resolved cell outlines
are returned as WGS84 GeoJSON and overlaid on the map. Dense output is written
at 10 m in the grid's actual local UTM CRS, which is shown in the plan and saved
in the catalog and raster metadata. Reference bounds are transformed from their
declared CRS before display; the browser never treats projected coordinates as
longitude/latitude.

The main Grid UI accepts vector drawing or a Shapefile. Select all Shapefile
components together or provide a ZIP, and always include `.prj`; the server
reads that CRS and reprojects the AOI to WGS84 before grid lookup and browser
display. The backend retains WGS84 WKT parsing for backward compatibility, but
the simplified browser does not advertise it.

Point CSV files must contain unique `sample_id` plus WGS84 `lon` and `lat`.
Point Shapefiles must include `.shp`, `.shx`, `.dbf`, and `.prj` and may contain
only Point/MultiPoint geometry. Polygon point inputs are rejected during
preflight rather than silently converted.

## Reliability and limits

- A fixed executor bounds both running and queued tasks; a full queue returns
  HTTP 429 rather than creating unbounded background threads.
- Every active task records its PID and process creation time. Cancellation
  terminates the full child process tree. Startup detects exact surviving
  children from a previous server instance, terminates them, and marks the task
  `interrupted`.
- Each task writes an atomic `task.json`, append-only `run.log`, and structured
  `events.jsonl` under its native state directory. The final reproducibility
  record is saved there as `download_state/web_task_report.json`.
- Progress events contain workflow, grid/chunk counters, rates, and ETA where
  available. The browser retains only the latest 1,000 lines; the full log
  remains available from the task card.
- A task cannot target an output directory already used by another active task.
- After a zero exit code, output validation opens the Parquet/Zarr/GeoTIFF products and
  checks catalogs, row/grid counts, dimensions, CRS, 10 m resolution,
  compression metadata, and expected paths. A task becomes `done` only when
  validation succeeds.
- The Results dialog exposes validation details, the exact command, plan, Git
  revision/dirty state, Python/platform/package versions, AEF dataset, year
  range, feature count, storage protocol, and MGRS-index checksum.

Optional environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `AEF_GRITS_WEB_STATE` | user application state directory | native-filesystem plans, PID, logs, checkpoints and reports |
| `AEF_GRITS_WEB_STAGING` | unset | native-filesystem per-task staging root; required for Linux NTFS output |
| `AEF_GRITS_WEB_OUTPUT` | `~/Downloads/AEF-GRiTS` | default root and Web folder-browser boundary |
| `AEF_GRITS_WEB_CONCURRENCY` | `2` | maximum simultaneous subprocesses |
| `AEF_GRITS_WEB_MAX_QUEUE` | `20` | maximum waiting tasks |
| `AEF_GRITS_WEB_MAX_GRIDS` | `500` | maximum grids in one plan |
| `AEF_GRITS_WEB_MAX_UPLOAD_MB` | `512` | request/upload limit |
| `AEF_GRITS_WEB_MAX_RAW_GIB` | `100` | hard uncompressed-size limit |
| `AEF_GRITS_WEB_WARN_RAW_GIB` | `10` | exact-phrase confirmation threshold |
| `AEF_GRITS_WEB_DISK_FACTOR` | `1.0` | raw-size multiplier used by disk guard |
| `AEF_GRITS_WEB_DISK_RESERVE_GIB` | `2` | free-space reserve after planned work |
| `AEF_GRITS_WEB_PLAN_TTL` | `3600` | signed plan lifetime in seconds |
| `AEF_GRITS_WEB_PLAN_SECRET` | local persistent secret | optional explicit signing secret |
| `AEF_GRITS_WEB_AUTH_MODE` | `local` | `local` or per-user `oauth` |
| `AEF_GRITS_WEB_SESSION_SECRET` | local persistent secret | Flask session signing secret |
| `AEF_GRITS_WEB_OAUTH_CLIENT` | unset | server-side OAuth client JSON path |
| `AEF_GRITS_WEB_OAUTH_REDIRECT_URI` | unset | exact HTTPS callback URI |
| `AEF_GRITS_AUTH_VAULT` | unset | encrypted SQLite vault on native storage |
| `AEF_GRITS_AUTH_VAULT_KEY` | unset | external owner-only vault key |
| `AEF_GRITS_WEB_ALLOWED_DOMAINS` | unset | optional Workspace-domain allowlist |
| `AEF_GRITS_WEB_TRUSTED_HOSTS` | localhost | accepted Flask host names |
| `AEF_GRITS_WEB_PROXY_COUNT` | `0` | trusted reverse-proxy hop count |

When `AEF_GRITS_WEB_OUTPUT` is on Linux NTFS/NTFS3, both `AEF_GRITS_WEB_STATE`
and `AEF_GRITS_WEB_STAGING` must point to ext4/XFS (or another native local
filesystem). The server refuses to start new work when it can see a related
uninterruptible D-state process. Active control files and the authoritative
catalog never use NTFS; only completed products are delivered there.

The Web folder browser accepts relative subdirectories only and rejects `..`.
Only an explicit local native-folder selection grants a destination outside the
configured root; that grant is validated and bound to the task plan. Local mode
is intended for one trusted user. OAuth mode is intended for an authenticated
campus deployment behind HTTPS, not an anonymous public download service.
