# AEF-GRiTS performance and function audit — 2026-08-13

## Outcome

The Python download environment, direct Earth Engine access, grid planning,
lossless Zarr storage, local resolver reads, web task controls, and the revised
L-shaped browser interface passed the available Windows-hosted audit. The code
is packaged for Python 3.12 on Linux and macOS, but a final native smoke test on
each target operating system is still required because this audit host is
Windows.

## Automated and online checks

| Check | Result |
|---|---|
| Full Python test suite | PASS — 60 tests |
| Browser/API suite included in the full run | PASS |
| Python byte-code compilation | PASS |
| Unix bootstrap script syntax (`bash -n`) | PASS |
| Conda environment YAML parse | PASS |
| Wheel build without dependency isolation | PASS |
| Wheel contains `mgrs.parquet`, doctor, and streaming scripts | PASS |
| Environment doctor | PASS — all point/grid dependencies |
| Earth Engine initialization | PASS — project `<redacted-project>` |
| PROJ transform | PASS — EPSG:4326 to EPSG:32650 |
| Zarr v3 float32 Zstd-7/no-shuffle round trip | PASS |
| Packaged authoritative MGRS index | PASS |
| MGRS `17MPU`, 2025 plan-only | PASS — EPSG:32717, 10980 x 10980 |
| Tessera `grid_-79.95_-1.05`, 2025 plan-only | PASS — EPSG:32717, 1114 x 1107 |

The `17MPU` plan reports 28.744 GiB of uncompressed float32 values for one
year. The Tessera plan reports 0.294 GiB. These are safety estimates, not final
losslessly compressed sizes.

## Local Zarr read benchmark

The benchmark used the real downloaded 2025 Tessera store
`grid_-80.05_-1.05`, chunks `(1,64,64,64)`, shards `(1,64,512,512)`, float32,
Zstd-7, and no shuffle. Every benchmarked store was checked for bit-exact
equality.

| Access pattern | Median | p95 |
|---|---:|---:|
| Single point | 2.31 ms | 2.66 ms |
| 9 x 9 patch | 1.76 ms | 2.67 ms |
| 256 x 256 inference window | 297.73 ms | 307.53 ms |

The decoded 256 x 256 throughput was approximately 1007 MiB/s on a warm local
filesystem cache. This is a resolver/storage benchmark, not an Earth Engine or
network throughput promise. The small point-versus-patch difference is within
the effects of chunk reuse and cache warming; both access patterns decode the
touched compressed chunks through the standard Zarr interface.

Raw results are stored in
`outputs/performance_audit_20260813/benchmark.json`.

## Browser acceptance

At a 1600 x 1000 viewport the sidebar occupies the full left edge and contains
only the download form. The task history occupies a 310-pixel dock below the
map. Six task cards remained in one row, older records overflowed horizontally,
and the dock reported `scrollWidth=2376` versus `clientWidth=1220`. The newest
task was first. Cards expose target, scope, years/CRS, storage path, output
validation, progress, and actions without vertical clipping.

A second 1280 x 800 layout check passed: the map remained 900 x 438 pixels, the
dock stayed directly below it, the first 380-pixel card fit vertically, and
horizontal overflow remained available.

The accepted rendering is
`webapp/acceptance/l_shaped_task_dock_20260813.png`.

## Reliability coverage

Tests cover signed expiring one-use plans, point preflight before confirmation,
server raw-size and disk limits, bounded queue saturation, duplicate output
prevention, exact process identity, process-tree cancellation, restart orphan
recovery, structured progress events, browser log truncation, authoritative CRS
and grid footprints, post-download product validation, and reproducibility
metadata. Grid blocks are checkpointed and safely rewritten when an incomplete
block is resumed; point Parquet shards and metadata use atomic replacement.
No machine-specific absolute path was found in the Python, shell, or environment
sources; paths are derived from CLI arguments, environment variables, the
repository root, or `pathlib.Path`.

## Remaining qualifications

1. Native Linux and Intel/Apple Silicon macOS execution was not possible on the
   Windows audit host. Run the documented doctor plus one-point and one-Tessera
   smoke tests on each actual target machine before a long job.
2. An isolated wheel build initially attempted to reach the configured Tsinghua
   PyPI mirror and failed with an SSL connection reset. The local non-isolated
   wheel build passed. This is a mirror/network condition, not a source-build
   failure; a fresh machine still needs working conda/PyPI connectivity.
3. `pip check` in the broad `py312_torch` environment reports an unrelated
   existing `pygis -> anymap` dependency gap. `environment-download.yml` does
   not install `pygis`; the dedicated AEF-GRiTS download environment avoids this
   unrelated package conflict.
4. The Windows host emits a `GDAL_DATA` discovery warning from its mixed GIS
   environment. CRS transformation and vector tests still pass. The dedicated
   conda-forge environment should supply GDAL data correctly; the doctor will
   fail on an actual transformation error.
5. The web application remains a trusted localhost, single-user tool. It is not
   hardened for public or multi-user deployment.
