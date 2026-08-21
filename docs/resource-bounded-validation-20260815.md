# Resource-bounded download validation — 2026-08-15

## Implemented controls

- Host, explicit-limit, and Linux cgroup-aware memory detection.
- Conservative grid/point peak-memory models and `--workers auto`.
- Bounded grid result windows and lazy bounded point-Future submission.
- Cross-process Earth Engine request tokens with a fixed pool contract.
- One cross-process final-delivery lease stored on native state storage.
- Web memory-weighted admission in addition to task-count and queue limits.
- Runtime peak RSS and throttle counters in plans and reports.
- High-watermark prefetch suppression and critical-watermark resumable stop.

## Automated validation

- Windows Server / Python 3.12: **77 passed, 1 POSIX-only test skipped**.
- Ubuntu 24.04 / ext4 / Python 3.12: **72 passed**.
- Tests cover cgroup precedence, unsafe explicit-concurrency rejection,
  memory-dependent auto workers, point year/chunk scaling, request-token
  concurrency, exclusive delivery locking, Web memory admission, Zarr staging,
  atomic checkpoints, interruption, resume, and NTFS isolation.
- A 1,024-block synthetic grid stress run completed with the number of live
  fetches never exceeding the planned in-flight bound.

## Low-memory planning check

A production MGRS `50RMU` 2025 plan was evaluated under an explicit 1 GiB
limit with a worst-case 433-pixel request block. The planner reserved 512 MiB,
left a 512 MiB usable budget, and reduced `workers=auto` to **1** with one
in-flight result. Estimated peak was 0.406 GiB; no download was started.

## Authenticated Earth Engine acceptance

Quota project: `<redacted-project>`.

Point path:

- Two Ecuador points, 2025, 64 dimensions, two concurrent one-row chunks.
- Both rows and all 64 features were complete.
- Download stage: 1.80 seconds.
- Observed peak RSS: 148.1 MiB; estimated peak: 304 MiB.
- Immediate rerun reused the committed delivery without another download.
- The unified point loader verified two shards, unique IDs, full schema, and
  100% completeness.

Grid path:

- Real 256 x 256, 10 m EPSG:32717 reference grid in 17MPU, year 2025.
- One 16 MiB `computePixels` response, 64 bands, 65,536 valid pixels.
- Authenticated download and final delivery: 28.15 seconds.
- Observed peak RSS: 188.5 MiB; estimated peak: 368 MiB.
- Output shape `(1, 64, 256, 256)`, chunks `(1, 64, 64, 64)`, 4,194,304
  finite values, Zstd-7/no-shuffle.
- Resume rerun completed the grid body in 0.06 seconds without downloading the
  completed block again.

## Acceptance conclusion

The bounded-memory and concurrent-download implementation passed synthetic,
cross-platform, low-memory planning, and authenticated point/grid acceptance.
Production throughput still depends on Earth Engine latency and the final
filesystem. Linux NTFS must remain final-delivery-only; resource state,
checkpoints, catalogs, logs, and active Zarr staging must stay on ext4/XFS.
