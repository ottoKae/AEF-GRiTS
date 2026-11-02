# Command and result contract

Run from the checkout using the chosen Python executable. Replace uppercase path
placeholders with explicit task paths before running. Keep credential/project
configuration private on the execution host; examples omit real identifiers.

## Inspect, authenticate, plan

```text
python -m scripts.stream_aef_grid_ee --help
python -m scripts.stream_aef_points_ee --help
python -m aef_grits.auth_cli status
```

`status` discovers credentials without logging in. When the task authorizes
online checking and a Project is configured privately:

```text
python -m aef_grits.auth_cli verify --source auto --check-aef
```

Downloads reuse Earth Engine credentials or ADC; explicit invalid sources fail
without switching identity. A private `AEF_GRITS_PROJECT` or a Project already
stored with credentials can avoid a command-line Project argument. Do not dump
environment variables or credential JSON into logs. Login is a separate,
explicit user action; see `docs/authentication.md`.

Offline environment and layout checks (these write small probe/state files):

```text
python -m aef_grits.doctor --mode grid --skip-ee --output OUTPUT_DIR --minimum-free-gib 2 --json
python -m scripts.check_aef_storage --final OUTPUT_DIR --state STATE_DIR --staging STAGING_DIR --timeout 10
```

`healthy` means layout/probes succeeded, not that this entire task fits. The grid
plan's free-space field measures staging-or-state, not necessarily final storage.
Inspect every actual filesystem separately. Don't run probes against a mount
already known to have uninterruptible I/O waits.

## Points

```text
python -m scripts.stream_aef_points_ee --samples SAMPLES.csv --years 2025 --out-dir OUTPUT_DIR --state-dir STATE_DIR --resource-state-dir RESOURCE_DIR --workers 1 --validate-only
```

Use `--layer` and `--id-field` where needed. For polygons, explicitly select
`--geometry-mode representative` or `--geometry-mode interior_pixels`; the latter
also needs `--reference-grid REFERENCE.tif` and an agreed `--max-points`.
The validator writes `point_preflight.sqlite` and validation JSON; keep planning
state separate from any active download. It does not query AEF availability.
Point stdout mixes `AEF_EVENT` lines with the report; read the saved validation
JSON rather than parsing the entire log as one JSON document.

After preflight and access/budget checks, use the same approved production
arguments without `--validate-only`. Defaults: chunks/pages of 1,000 points,
64 features per requested year, scale 10 m. Input order, IDs, coordinates,
normalized content, year list, chunk size and sampling parameters bind resume.
There is no `--resume` switch. Repeating a matching command reuses valid shards.
Never use `--overwrite` to bypass a signature mismatch; it is not year append.

## Grids

```text
python -m scripts.stream_aef_grid_ee --grid-scheme mgrs --tiles 50RMU --years 2025 --out-dir OUTPUT_DIR --state-dir STATE_DIR --resource-state-dir RESOURCE_DIR --workers 1 --plan-only
python -m scripts.stream_aef_grid_ee --grid-scheme tessera_0p1 --tessera-tile 114.05 30.55 --years 2025 --out-dir OUTPUT_DIR --state-dir STATE_DIR --resource-state-dir RESOURCE_DIR --workers 1 --plan-only
python -m scripts.stream_aef_grid_ee --grid-scheme reference --reference REFERENCE.tif --tile-id pilot --years 2025 --out-dir OUTPUT_DIR --state-dir STATE_DIR --resource-state-dir RESOURCE_DIR --workers 1 --plan-only
```

These are alternative selectors, not three jobs to launch automatically.
For a bounded grid pilot use an explicitly small projected 10 m reference TIFF;
the streamer has no maximum-blocks flag. A complete 0.1° tile or MGRS tile is
not automatically a small test. Creating that reference is a local write, so
do not do it during a strictly read-only review.
For AOI files first use `python -m scripts.resolve_aef_grid_ids --help`; its
`--aoi`, `--schemes mgrs tessera_0p1`, `--out-dir` generate the grid list. Freeze
the selected IDs; a huge bbox can expand into many complete 0.1° tiles.

After the requested stage is authorized and checked, remove `--plan-only`.
Optional formats: `--export-geotiff` keeps Zarr plus yearly TIFF;
`--geotiff-only` delivers TIFF/metadata using a temporary Zarr cache. They are
mutually exclusive. TIFF-only requires `--out-dir`, cannot use `--staging-dir`
or legacy adoption flags, and needs peak space for cache plus TIFF.
Both TIFF modes reject Linux NTFS final output.

Keep `--block-size 256 --inner-chunk 64 --shard-size 512` for ordinary runs.
Grid signature binds the full sorted year list, CRS/affine/shape, grid identity,
block/chunk/shard, codec and `native-utm-v1` source manifest (including asset
versions). Repeat the same command for resume;
changing years is not in-place append. Use new batch roots for a changed year
list/layout. The filename only shows min/max year, and catalog keys omit years:
do not share one catalog between different annual products for the same grid.
`--adopt-existing-complete` / `--import-legacy-progress` are explicit recovery
operations for matching complete ledgers with native-source evidence, not generic
retry switches. Pre-native products cannot be adopted as native; use a new output
directory. Completed TIFF-only recovery validates frozen manifests offline.

## Resources

Both streamers support `--workers auto|N`, `--memory-limit-gib`,
`--memory-reserve-gib`, `--global-request-limit`, `--resource-state-dir`,
`--resource-profile`, `--max-retries`, `--request-timeout-seconds`.
Profiles: `workstation-auto`, `low-memory-1g`, `server-8g`, `server-16g`.
Choose from measured host/cgroup headroom, not the server's marketing RAM.
`--memory-limit-gib` caps the headroom used by the planner; reserve is subtracted
afterwards. It is not an OS-enforced RSS limit for the process.
Keep a reserve for other processes; do not sum per-process “auto” budgets as if
they were a global reservation. Request limits only cooperate at the same root;
an existing token-pool limit must match. Do not create another root to evade it.

Grid adaptive request splitting handles repeatedly truncated responses without
changing checkpoint layout. It does not fix sustained NIC loss or a stuck disk.
Module exits of 75 indicate a resumable request failure, not permission for an
unbounded relaunch loop. Inspect authentication errors separately; do not retry
authorization failures as ordinary transport failures.

## Results and validation

| Product | Main artifacts |
|---|---|
| Points | `OUTPUT_DIR/shards/*.parquet`; `STATE_DIR/catalog.parquet`, `report.json`, `run.json`, `delivery.json` |
| Grid Zarr | `OUTPUT_DIR/GRID_ID/aef_GRID_ID_MINYEAR_MAXYEAR.zarr`; `STATE_DIR/catalog.parquet`, `run_summary.json`, `grids/GRID_ID/*.progress.json` |
| TIFF-only | yearly TIFF files and `geotiff_manifest.json`; control catalog/reports, no permanent Zarr promised |

Without explicit state, defaults differ: grid uses `OUTPUT_DIR/.aef_state`,
points can use the output root on a native filesystem. Prefer explicit roots.
Include control metadata in a handoff; the Zarr directory alone is not the full
resume bundle. Catalogs can contain absolute paths: copying data does not make
those paths portable. Verify/remap paths on the destination in a separate
delivery catalog, preserve originals and do not edit an active catalog.

Use the existing structural validator with a private JSON payload:

```json
{
  "output_dir": "ABSOLUTE_OUTPUT_DIR",
  "control_dir": "ABSOLUTE_STATE_DIR",
  "params": {"workflow": "grid", "grid_ids": ["50RMU"], "years": [2025], "output_format": "zarr"}
}
```

```text
python -m aef_grits.delivery_validation --input PAYLOAD.json --output VALIDATION.json --final ABSOLUTE_OUTPUT_DIR
```

For points set workflow to `points`, supply the expected `sample_count` and
years, and omit grid IDs. With a custom `--catalog`, inspect its actual path:
the validator expects the conventional catalog in the control directory.
Read `valid`, `errors`, `warnings`; exit zero can accompany `valid:false`.
It checks structure/reports, not all values or every stored chunk. Independently
read requested point batches with `open_aef_point_dataset(...).iter_batches()`
or grid windows with `aef_grits.store.AEFZarr`; compare IDs/years, all 64 values,
CRS/transform, finite/NaN counts and expected source masks. State the sampled
extent or perform a bounded full audit when required. TIFF catalogs are not
Zarr resolver catalogs; read TIFF with Rasterio/QGIS.

Raw float32 feature bytes, actual file bytes, filesystem allocation and network
traffic are different quantities. Point tables may store float64 columns, so
the float32 estimate is not their exact size. NoData may compress very well but
is not usable coverage. Existing `scripts/check_aef_coverage.py` queries source
masks online, has no offline mode, and does not verify downloaded files.
