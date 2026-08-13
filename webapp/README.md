# AEF-GRiTS local web application

This localhost-only application plans and runs the existing AEF-GRiTS point
and dense-grid streaming commands. It does not maintain a second download
implementation: every accepted plan is translated into the tested command-line
interface.

## Install, test, and start

From the repository root:

```powershell
python -m pip install -e ".[gee,geo,stream,web]"
python webapp/app.py
```

Open `http://127.0.0.1:5555`. The server uses the Earth Engine credentials and
Google Cloud project of the selected Python environment.

Run the API/unit suite and the four browser workflow tests with:

```powershell
python -m pip install -e ".[gee,geo,stream,web,web-e2e]"
python -m pytest tests/test_webapp.py tests/test_webapp_e2e.py -q
```

The Playwright tests reuse an installed Chromium/Chrome executable when one is
available. They use synthetic command execution; they do not download Earth
Engine data.

## Two-stage protocol

The main screen is intentionally a three-step form:

1. choose **Points** or **Grid**;
2. choose a CSV/point Shapefile, or choose MGRS/Tessera and set an AOI;
3. choose years and an output subdirectory, then press **Download**.

Technical retry, chunk, checkpoint and CRS settings are not shown in the main
form. Safe tested defaults are used. Capacity planning still happens after the
single Download click and before the confirmation dialog.

The left sidebar is reserved for defining a new download. Task history is a
single horizontal dock below the map, forming an L-shaped workspace with the
sidebar. The newest task is always placed at the left. Arrow controls and native
horizontal scrolling expose older tasks without adding a second row. Each card
shows the download target, spatial scope or source file, years, output CRS,
storage path, product-validation status, and progress.

Native browser file inputs are visually replaced by consistent large picker
tiles. The CSV and Shapefile modes still use the browser's secure file chooser,
but the chosen names are reported inside the designed tile. Grid AOI import and
vector drawing use the same icon-first layout. Output selection opens a
server-side directory browser rooted strictly at `AEF_GRITS_WEB_OUTPUT`; it can
navigate or create subdirectories but cannot escape that root. This distinction
is intentional: a browser-side directory picker cannot grant the Python server
a trustworthy writable filesystem path.

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
compressed-storage promise.

## Workflow and CRS contract

| Workflow | Selection | Local output |
|---|---|---|
| Points | CSV or Point/MultiPoint Shapefile | sharded Parquet |
| MGRS | exact AOI intersection with packaged `aef_grits/data/mgrs.parquet` | one UTM Zarr per MGRS grid |
| Tessera 0.1 degree | positive-area AOI intersection with 0.1-degree cells | one UTM Zarr per Tessera cell |

The browser exposes only MGRS and Tessera dense grids. Reference grids remain
available through the documented Python/CLI API for advanced reproducible work.

The browser/server exchange AOI geometry in `EPSG:4326`. Leaflet displays it in
Web Mercator (`EPSG:3857`) only for visualization. Server-resolved cell outlines
are returned as WGS84 GeoJSON and overlaid on the map. Dense output is written
at 10 m in the grid's actual local UTM CRS, which is shown in the plan and saved
in the catalog/Zarr metadata. Reference bounds are transformed from their
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
  `events.jsonl` under `webapp/runs/<run_id>/`. The final reproducibility record
  is saved as `<output_dir>/web_task_report.json` beside the products.
- Progress events contain workflow, grid/chunk counters, rates, and ETA where
  available. The browser retains only the latest 1,000 lines; the full log
  remains available from the task card.
- A task cannot target an output directory already used by another active task.
- After a zero exit code, output validation opens the Parquet/Zarr products and
  checks catalogs, row/grid counts, dimensions, CRS, 10 m resolution,
  compression metadata, and expected paths. A task becomes `done` only when
  validation succeeds.
- The Results dialog exposes validation details, the exact command, plan, Git
  revision/dirty state, Python/platform/package versions, AEF dataset, year
  range, feature count, storage protocol, and MGRS-index checksum.

Optional environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `AEF_GRITS_WEB_OUTPUT` | `webapp/output` | authoritative output root |
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

The output field accepts a relative subdirectory only. Absolute paths and `..`
are rejected, so a browser request cannot write outside the configured output
root. The Flask server is intended for one trusted local user and is not a
public or multi-user deployment service.
