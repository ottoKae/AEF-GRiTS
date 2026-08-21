# Download hardening implementation and validation

Date: 2026-08-16  
Baseline: `7453472`  
Branch: `fix/ntfs-safe-pipeline-20260815`

## Implemented

### P0 - request reliability

- Every direct Earth Engine attempt has an explicit client deadline through
  `ee.data.setDeadline`; the default is 300 seconds.
- Earth Engine's hidden retry loop is disabled, and AEF-GRiTS owns retry
  classification, bounded exponential backoff, jitter and structured events.
- Timeout and transient failures preserve committed point shards or the grid
  ledger and use resumable exit status 75.
- Permanent authentication, permission, invalid-expression and schema failures
  are not retried blindly.

### P0 - streaming point sources

- CSV uses Pandas chunks and Parquet uses Arrow record batches.
- Shapefile, GeoPackage, GeoJSON and GeoParquet use bounded Pyogrio feature
  batches. Polygon interior pixels are emitted in bounded batches.
- Exact duplicate ID/coordinate checks use append-only SQLite heaps plus one
  disk-backed `GROUP BY`, stored on the native state filesystem.
- Preflight creates coordinate and full normalized-content hashes. Input order
  deterministically defines Parquet shard membership.
- Web preflight no longer retains the complete point table.

### P1 - out-of-core point results

- `open_aef_point_dataset()` validates catalog, shard row counts, schema, years
  and completeness without concatenating all shards.
- `iter_aef_point_batches()` and the dataset handle support batch size, column
  projection, year projection and Arrow filters.
- Exact cross-shard duplicate-ID auditing is optional and disk-backed.
- The original `load_aef_points()` remains available for small datasets.

### P1 - resource profiles and telemetry

- Profiles: `workstation-auto`, `low-memory-1g`, `server-8g`, `server-16g`.
- Reports now include request latency p50/p95/max, retry and timeout counts,
  bytes received, request-token wait, Zarr/Parquet write time and final-delivery
  time.
- `aef-grits-benchmark-summary` combines explicitly selected reports without
  recursively scanning a delivery filesystem.

### P2 - Web scheduling and visibility

- Web admission jointly enforces memory capacity and maximum active tasks.
- Small tasks may backfill when the oldest request cannot currently fit.
- Backfill stops after an aging threshold so the oldest large task cannot
  starve.
- Task responses and cards expose reservation, resource profile, request limit,
  retries and current admission state.

### P2 - cross-platform certification

- GitHub Actions now defines Python 3.11/3.12 jobs on Ubuntu, Windows and
  macOS, including package installation, all tests, command entry points,
  synthetic Zarr I/O and a low-memory MGRS plan.
- Authenticated Earth Engine acceptance remains separate from credential-free
  CI.

## Validation results

### Automated tests

- Windows Python 3.12: **90 passed, 1 POSIX-only skipped**, including all six
  Playwright workflows.
- Ubuntu 24.04, Python 3.12, native ext4: **85 passed**; browser tests were not
  collected because Playwright is not installed in that local Linux test env.
- Existing 1,024-block bounded grid stress coverage remains green.

### One-million-point preflight

Input: 1,000,000 CSV rows, one year, 10,000-row chunks,
`low-memory-1g` profile.

| Implementation | Elapsed | Peak RSS |
|---|---:|---:|
| indexed per-row SQLite UPSERT | 157.90 s | 105.36 MiB |
| append-only audit plus disk `GROUP BY` | 6.37 s | 163.74 MiB |
| final version with full-content signature | 9.80 s | 162.95 MiB |

The final implementation is below the 512 MiB usable budget and is about 16
times faster than the first exact audit implementation.

### Authenticated Earth Engine acceptance

Project: `<redacted-project>`.

Point run:

- two Ecuador points, 2025, two concurrent one-row chunks;
- 2/2 complete, no retry or timeout;
- request latency p50 1.77 s, p95 1.97 s;
- observed peak RSS 150.66 MiB versus estimated 304 MiB.

Grid run:

- real 256 x 256, 10 m, EPSG:32717 reference grid, 2025, 64 bands;
- one 16 MiB `computePixels` response and 65,536 valid pixels;
- request latency 13.21 s, no retry or timeout;
- Zarr write time 0.12 s;
- observed peak RSS 194.30 MiB versus estimated 368 MiB.

The two reports were successfully combined by the benchmark summarizer.
`calibration_ready` correctly remains false because a recovered-server Tessera
run and one complete MGRS-year are still required.

## Fixed contracts retained

- 10 m float32 Zarr v3;
- chunks `(1,64,64,64)` and shards `(1,64,512,512)`;
- lossless Zstd level 7 with no shuffle;
- one coordinator Zarr writer;
- native-filesystem state, logs, checkpoints, catalogs and active staging;
- Linux NTFS3 only as a validated immutable final-delivery target;
- cross-process request tokens and one final-delivery lease.

## External validation still pending

The code work is complete, but two claims require external infrastructure:

1. The production server remains unavailable over SSH until its NTFS3 incident
   is resolved out-of-band. Tessera, concurrent-server, complete MGRS-year and
   legacy `50RMU` adoption tests must wait for recovery.
2. The macOS workflow is implemented but cannot be declared passed until it is
   pushed and a real GitHub macOS runner completes it.

No production limits should be relaxed before the server calibration sequence
passes. No GitHub push was performed as part of this implementation.
