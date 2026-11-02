---
name: aef-grits-download
description: Plan, run, monitor, resume and verify AEF-GRiTS point or grid downloads on a local computer or an SSH host using this repository. Use for annual AlphaEarth feature acquisition and delivery, not S1/S2 production, Tessera embeddings or model training.
---

# AEF-GRiTS download operations

Turn the user's AEF request into a bounded, reproducible download using the
existing CLI. This skill is shipped with the source repository and needs no
other GRiTS repo, global skill install, cloud scheduler or public Web server.

## Locate and scope

- Locate the checkout containing `pyproject.toml`, `aef_grits/` and `scripts/`;
  use that directory as the working directory. Paths below are checkout-relative.
  On SSH, inspect the remote checkout/environment independently; the local
  version and credentials do not establish the remote ones.
- Distinguish planning, read-only inspection, authorized pilot/production,
  recovery and transfer. A request to configure this skill does not authorize
  a real job. Within an already authorized scope, proceed without repeatedly
  asking for the same approval; ask only for missing choices that change it.
- Record the execution host/SSH alias, interpreter, source revision and dirty
  code hashes, years, points/AOI/grid IDs, format, output/state/staging roots,
  shared resource root, memory/request budgets and existing-data policy in a
  private task receipt. Record the actual year list, not just filename endpoints.
  No real Project IDs, tokens, private keys or private SSH addresses in public docs.
- Reuse existing catalogs/products before downloading another copy. Respect the
  user's storage policy; do not put research data in a source checkout merely
  because the CLI defaults to `./outputs`. Never assume another user's disk paths.

## Route to the existing implementation

Read [commands.md](references/commands.md) for commands, outputs, resource and
resume details. For SSH also read [ssh.md](references/ssh.md). Check current
`--help` before execution; examples are not a substitute for the current parser.

| Need | Existing entry point |
|---|---|
| Credential discovery / explicit login / access verification | `python -m aef_grits.auth_cli` |
| Environment check | `python -m aef_grits.doctor` |
| AOI to grid IDs | `python -m scripts.resolve_aef_grid_ids` |
| Points to Parquet | `python -m scripts.stream_aef_points_ee` |
| MGRS / 0.1° / reference grid to Zarr or TIFF | `python -m scripts.stream_aef_grid_ee` |
| Storage layout / free-space probes | `python -m scripts.check_aef_storage` |
| Structural delivery check | `python -m aef_grits.delivery_validation` |

Prefer module invocations in the verified environment: the streamers' exit-75
mapping is in their `__main__` blocks, not their console-entry `main` functions.
This skill adds no runtime scheduler, distributed memory reservation or SSH daemon.

## Data contract

- Production source is Earth Engine `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`,
  64 bands `A00`–`A63`. Current supported point years are 2017–2025; do not infer
  another year's availability from a successful offline grid plan. Anonymous
  public-source GUI downloading is not a completed alternative in this checkout.
- CSV/Parquet points require unique `sample_id` and WGS84 `lon,lat`; vector
  inputs require a declared CRS. Polygon representative points and all interior
  pixels are different tasks: choose explicitly, with a point-count budget.
- Point coordinate columns in EPSG:4326 are not an output raster CRS. New
  `native-utm-v1` downloads sample the containing source UTM pixel with its
  explicit projection and no scale override. Per-year source asset/CRS/pixel
  coordinates and per-shard `.native.json` retain provenance. Inspect these
  records before comparing with a raster in a different UTM zone.
- MGRS uses the packaged authoritative index; 0.1° is a geographic selection
  unit with a projected output grid, not a request for Tessera features. A bbox
  selects whole tiles, not a clipped polygon. Reference mode requires a
  native-compatible UTM 10 m GeoTIFF. Source metadata are checked online before
  writing; offline plans remain unverified. Cross-UTM filling and fractional
  pixel shifts are refused. Inspect CRS, transform and shape before claiming
  alignment with S1/S2 or another source. Old downloads have no retroactive
  native guarantee: keep them read-only and use a new batch directory.
- Zarr is `[year,64,y,x]`, float32, chunks `(1,64,64,64)`, shards
  `(1,64,512,512)`, Blosc/Zstd-7/no-shuffle. Keep defaults unless the task requires
  a change. Request blocks default to 256; a 512² × 64 float32 request exceeds
  the configured 48 MB response ceiling. Do not copy S2's dtype/codec defaults.

## Execute only the requested stage

1. **Inspect and plan.** Read existing data/state and identify active writers.
   `--plan-only`, `--validate-only`, doctor and storage-check can create local
   files; they are offline checks, not read-only inspection. Use a fresh small
   planning directory, not active production state. No Earth Engine verification
   request when the user explicitly asks for offline-only work.
2. **Check access and resources.** Verify existing credentials on the execution
   host, then small AEF access if authorized. Missing credentials require the
   user's explicit login action; never log in during download/retry. Compute
   capacity per filesystem including staging, TIFF peak copies, transfer and
   reserve. Grid planning reports raw bytes, not a disk-capacity pass or a
   measured compressed size. Preserve all years/bands; do not silently lower
   resolution to fit a budget.
3. **Pilot then run.** Reuse comparable pilot evidence where valid. Otherwise
   propose a bounded pilot, not an unrequested complete MGRS download. Prefer one
   streamer per host and sequential grid batches initially; workers parallelize
   requests. Cooperating processes must share one resource root and compatible
   request limit. CLI memory budgets are per-process, not global admission.
   Never run two writers for the same output/state/catalog.
4. **Monitor and recover.** Save exact commands, PID/service identity, logs and
   structured `AEF_EVENT` progress on a healthy native filesystem. Compare new
   committed blocks/shards over time, not PID existence alone. Preserve failure
   evidence; resume only matching inputs/settings/state, with no old writer.
   No unbounded relaunch loop, forced overwrite, checkpoint deletion or hidden
   switch of identity/source. On a filesystem incident stop further dispatch;
   do not reboot/remount or alter global network settings without authority.
5. **Verify and hand off.** Compare requested grids/years/IDs to reports,
   catalogs, ledger and actual files. Read `valid/errors/warnings` from delivery
   validation JSON: its exit code alone is insufficient. Use existing readers
   for real windows or point batches, inspect finite/NaN coverage and state
   sampling extent. All-NaN source coverage can still be structurally complete.
   If transfer was requested, verify the destination separately and resolve
   catalog paths there; do not count remote production as local delivery.

## Known boundaries to check, not silently promise away

- A committed ledger may be trusted on resume without reopening final files.
  Missing data requires investigation, not deleting the ledger to force a redo.
- Grid CLI catches per-grid exceptions and may continue to another grid;
  it is not universally fail-fast on a storage incident. Use isolated one-grid
  invocations when failure containment matters and inspect before dispatching more.
- API attempt deadlines do not bound the entire initialization/authentication
  phase. A background process being alive does not prove initialization completed.
- Structural validation is not a full data-integrity or coverage audit.
  Remote/source mask coverage queries also do not validate local files.

Final handoff: requested/completed/failed/missing coverage, actual execution host,
data and control roots, catalog/report paths, byte units and measured vs estimated
sizes, current stage/time, tested readers and any unresolved limitations. Never
report “complete” solely because a command returned zero or a service was started.
