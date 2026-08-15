# AEF-GRiTS download hardening code plan

Date: 2026-08-15  
Baseline: `7453472 feat(download): enforce resource-bounded concurrency`  
Branch: `fix/ntfs-safe-pipeline-20260815`

Implementation status (2026-08-16): work packages A-E are implemented and
locally validated. Package F is implemented as CI configuration; a real macOS
runner result remains pending. Production-server calibration remains blocked
by the out-of-band NTFS3 recovery. See
`docs/download-hardening-validation-20260816.md`.

## 1. Objective

Finish the remaining high-value reliability and scalability work after the
resource-bounded and NTFS-safe pipeline changes. The implementation must keep
memory bounded on Windows, macOS and Linux, make stalled Earth Engine calls
recoverable, support very large point inputs without loading the complete
table into RAM, and retain reproducible resume semantics.

The following production contracts are already accepted and will not be
redesigned:

- grid arrays remain 10 m float32 Zarr v3;
- chunks remain `(1, 64, 64, 64)` and shards remain `(1, 64, 512, 512)`;
- compression remains lossless Zstd level 7 with no shuffle;
- Earth Engine fetches may be concurrent, but Zarr has one coordinator writer;
- state, logs, checkpoints, catalogs and active staging stay on ext4/XFS/APFS
  or the native Windows filesystem;
- Linux NTFS3 is final-delivery-only and receives validated immutable products;
- `workers=auto`, cross-process request tokens and the final-delivery lease
  remain the governing concurrency controls.

## 2. Priority and implementation order

| Priority | Work package | Why it comes now |
|---|---|---|
| P0 | A. Earth Engine request deadlines | One blocked HTTP/RPC request can otherwise occupy a worker indefinitely. |
| P0 | B. Streaming point-source preflight and download | Current CSV/Parquet/vector point preflight materializes the full table. |
| P1 | C. Streaming point-result reader | The current unified loader concatenates every Parquet shard into RAM. |
| P1 | D. Server profiles, telemetry and calibration | Real-server evidence is needed before relaxing conservative limits. |
| P2 | E. Web queue fairness and resource visibility | Safety is already enforced, but queue order and operator visibility can improve. |
| P2 | F. macOS CI and packaging acceptance | Logic is cross-platform, but a real macOS runner has not yet certified it. |

Each package is independently testable. Packages A and B must be complete
before production-scale point or multi-tile runs are recommended.

## 3. Work package A - bounded Earth Engine request time

### Design

1. Add a shared request policy in `aef_grits/earth_engine.py` containing:
   - request deadline in seconds;
   - maximum retries;
   - exponential backoff with bounded jitter;
   - error classification for retryable transport/quota/server failures versus
     permanent request/schema/authentication failures.
2. Add CLI option `--request-timeout-seconds` to both direct streamers and pass
   it through the Web plan and child command.
3. Configure the supported Earth Engine client deadline with
   `ee.data.setDeadline(milliseconds)` immediately after initialization.
4. Record the deadline and retry policy in `run.json`, reports, Web plans and
   provenance.
5. Emit structured events for `request_started`, `request_retry`,
   `request_timeout` and `request_failed` without exposing credentials.
6. On final failure, preserve all committed point shards or the grid ledger and
   return a distinct resumable exit status. Never delete remote-filesystem
   temporary paths during this path.

### Files

- `aef_grits/earth_engine.py`
- `scripts/stream_aef_points_ee.py`
- `scripts/stream_aef_grid_ee.py`
- `webapp/app.py`
- `tests/test_direct_streaming.py`
- `tests/test_point_workflow.py`
- `tests/test_webapp.py`

### Acceptance gates

- A deliberately blocked fake request stops within deadline plus a small
  scheduler margin.
- Retryable failures retry the configured number of times with bounded delay.
- Permanent authentication/schema failures do not retry.
- A timed-out grid run resumes from its last durable block.
- A timed-out point run reuses its already committed shards.
- Request tokens are released after every success, exception and timeout.

## 4. Work package B - bounded-memory point ingestion

### Design

Introduce a `PointSource` abstraction with two separate passes:

1. **Streaming preflight pass**
   - validate required fields, finite coordinates, WGS84 range, IDs, years and
     requested feature count;
   - compute row count, bounds, stable source signature, duplicate-ID count and
     duplicate-coordinate diagnostics incrementally;
   - retain only bounded duplicate-detection state, using partitioned hashes on
     native state storage when the in-memory threshold is exceeded;
   - project only required columns unless the user explicitly asks to preserve
     selected metadata columns.
2. **Streaming execution pass**
   - reopen the source and yield deterministic chunks;
   - assign stable chunk IDs and row ordinals;
   - verify the second-pass signature against the confirmed plan;
   - keep at most `ResourcePlan.max_in_flight` chunks alive;
   - write one atomic Parquet shard per completed chunk.

Format strategy:

- CSV: `pandas.read_csv(..., chunksize=N, usecols=...)`;
- Parquet: `pyarrow.dataset` or `ParquetFile.iter_batches` with column
  projection;
- GeoParquet: Arrow batches where practical, with CRS-aware conversion;
- Shapefile, GeoPackage and GeoJSON: Fiona/Pyogrio feature batches where
  available; run vector conversion in a bounded isolated process because
  geometry decoding and polygon interior-pixel expansion can be expensive;
- polygon `interior_pixels`: stream polygon-by-polygon and stop before
  `max_points`; never build all generated point geometries in one list.

The public CLI and output schema remain compatible. The Web point protocol
continues to be preflight, signed confirmation, then execution; execution no
longer retains the preflight DataFrame.

### Files

- new `aef_grits/point_source.py`
- refactor `aef_grits/points.py`
- `scripts/stream_aef_points_ee.py`
- `webapp/app.py`
- `scripts/prepare_aef_points.py`
- `tests/test_point_workflow.py`
- `tests/test_webapp.py`
- new large-input stress fixtures generated during tests, not committed as data

### Compatibility rules

- `sample_id`, `lon`, `lat` remain mandatory in normalized point chunks.
- Input row order defines deterministic shard membership.
- Existing small-file calls returning a DataFrame remain available as a
  convenience API.
- Resume signatures must be content-derived, not based only on file path,
  modification time or size.
- A source changed after Web confirmation must be rejected before any request.

### Acceptance gates

- Synthetic 1,000,000-row CSV and Parquet preflight completes with bounded RSS;
  target peak is less than 512 MiB under the explicit 1 GiB test profile.
- No complete input DataFrame is retained during execution.
- Duplicate IDs spanning distant chunks are detected.
- Shard membership and signature are identical across reruns and worker counts.
- Existing point output and resume tests remain bit-for-bit compatible at the
  schema and row-order level.
- Polygon expansion obeys `max_points` without constructing an oversized list.

## 5. Work package C - out-of-core point-result access

### Design

Keep `load_aef_points()` for small datasets and add:

- `open_aef_point_dataset()` returning a validated PyArrow Dataset-backed
  handle;
- `iter_aef_point_batches()` with year and metadata column projection,
  optional filters and configurable batch size;
- catalog-only integrity validation that checks signatures, shard existence,
  row counts and schemas without concatenating all data;
- optional exact duplicate-ID audit using the same bounded partitioned-hash
  mechanism as point preflight.

### Files

- `aef_grits/point_store.py`
- package exports in `aef_grits/__init__.py`
- `tests/test_point_workflow.py`
- `README.md` and `README.zh-CN.md`

### Acceptance gates

- A multi-shard million-row fixture can be scanned batchwise under the 1 GiB
  profile.
- Year/column projection reads only selected Parquet columns.
- Missing shards, schema drift, row-count mismatch, duplicate IDs and missing
  requested years fail with actionable messages.

## 6. Work package D - production profiles and calibration

### Design

1. Add explicit conservative profiles, without hiding their resolved values:
   - `low-memory-1g`;
   - `server-8g`;
   - `server-16g`;
   - `workstation-auto`.
2. Add a machine-readable benchmark report containing:
   - detected host/cgroup memory;
   - resolved workers and in-flight requests;
   - peak parent and child RSS;
   - request latency percentiles and retry counts;
   - Earth Engine bytes received;
   - Zarr write/compression time;
   - staging-to-final copy time and throughput;
   - throttle time and token wait time.
3. Keep the estimator conservative until at least one point job, one Tessera
   cell and one complete MGRS-year have been observed on the recovered server.
4. Store calibration reports on the native state filesystem. Never discover
   metrics by recursively scanning an NTFS output tree.

### Server acceptance sequence

1. Check boot time, `/proc` D-state processes, mounts and NTFS3 kernel logs.
2. Run the storage doctor without touching an unhealthy NTFS mount in-process.
3. Run a 256 x 256 reference grid entirely on ext4.
4. Run one Tessera cell with ext4 state/staging and final delivery only after
   the filesystem health probe passes.
5. Run two concurrent Tessera tasks and verify global token/memory limits.
6. Run one complete MGRS tile-year.
7. Validate and adopt the existing `50RMU` 1849/1849 ledger without deleting or
   redownloading its 23 GB product.

### Acceptance gates

- No D-state process is created by the pilot sequence.
- Observed RSS remains below the critical budget and agrees with the report.
- The final-delivery lease permits only one final copy/rename at a time.
- Process interruption and restart resume without duplicate Earth Engine work.
- Estimator changes, if any, are based on recorded p95 observations plus a
  documented safety margin.

## 7. Work package E - Web queue fairness and observability

### Design

- Replace strict head-of-line memory admission with bounded backfilling: an
  earlier large task keeps priority, but smaller tasks may run when they fit;
  aging prevents starvation.
- Display resolved worker count, memory reservation, active/global request
  tokens, throttle status, retry count and final-delivery-lock state.
- Keep the current simple download form unchanged; operational details belong
  in the task dock and result view.
- Add warnings when the configured Web state, staging or resource root is not a
  native filesystem.

### Acceptance gates

- A large queued task cannot starve indefinitely behind repeated small tasks.
- Aggregate running reservations never exceed the Web budget.
- Browser state matches persisted server state after restart.
- Existing four Playwright workflows remain green.

## 8. Work package F - macOS certification

### Design and gates

- Add GitHub Actions for Python 3.11 and 3.12 on Ubuntu, Windows and macOS.
- Run unit tests, package installation, CLI help, a plan-only MGRS job and a
  local synthetic Zarr write/read test.
- Keep authenticated Earth Engine acceptance manual or secret-gated.
- Document APFS state/staging behavior and process cancellation expectations.

macOS support is declared certified only after the real macOS CI job passes;
until then it remains supported by design and local packaging, not by a real
runner result.

## 9. Test matrix

| Layer | Required tests |
|---|---|
| Unit | deadline configuration, retry classification, streaming statistics, partitioned duplicate detection, batch reader, queue aging |
| Fault injection | hung request, retryable 429/5xx, permanent auth error, memory high/critical pressure, interrupted final delivery |
| Stress | 1M point rows, 1,024 grid blocks, many Parquet shards, concurrent Web tasks |
| Cross-platform | Windows, Ubuntu/ext4 and macOS/APFS packaging and synthetic I/O |
| Authenticated | two points, 256 x 256 grid, one Tessera cell, then one MGRS-year |
| Recovery | interrupted point run, interrupted grid run, Web restart, legacy 1849/1849 adoption |

No stress fixture or authenticated output is committed to Git. Reports contain
configuration and measurements but no credentials.

## 10. Delivery strategy

Use one local commit per work package so each change can be reviewed or
reverted independently:

1. `fix(ee): bound direct request duration`
2. `feat(points): stream source validation and chunking`
3. `feat(points): add out-of-core result access`
4. `feat(ops): add server profiles and calibration telemetry`
5. `feat(web): improve fair resource scheduling`
6. `ci: certify cross-platform downloader`

After each P0 commit, run the complete local suite plus the low-memory stress
tests. Do not push any commit until explicitly authorized. Do not start Tier D
server acceptance until SSH has recovered and the administrator-level mount
checks are complete.

## 11. Immediate next implementation

Begin with work package A. It is small, isolated and closes the only remaining
path where a network request can hold a worker forever. Then implement work
package B before adding any new production point workload. Packages C through F
must not delay these two P0 reliability fixes.
