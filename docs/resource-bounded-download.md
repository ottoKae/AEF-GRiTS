# Resource-bounded concurrent downloads

AEF-GRiTS limits concurrency at four levels: Web task admission, process-wide
memory planning, cross-process Earth Engine request tokens, and an exclusive
final-delivery lease. This prevents independent values such as task count and
`--workers` from multiplying into an unbounded server workload.

## Memory detection

The effective available memory is the smallest of:

- host available memory reported by `psutil`;
- Linux cgroup v2/v1 headroom when present;
- `--memory-limit-gib` when explicitly configured.

An automatic reserve is removed before work is admitted. `--workers auto`
then selects a CPU-, request-, and memory-bounded worker count. An explicit
worker count is rejected when its conservative peak estimate reaches the
critical watermark.

For grids, the model includes the `computePixels` response, stacked float32
block, bounded completed-result queue, one uncompressed 512-pixel Zarr shard,
and compression scratch. For points, it includes chunk rows, requested years,
64 dimensions, Pandas expansion, and the bounded active-Future set.

## Cross-process controls

All cooperating jobs must use one native resource-state root:

```bash
export AEF_GRITS_RESOURCE_STATE=/home/user/aef_state/.resource_locks
```

The root contains advisory lock files only. Process death releases the OS lock;
no PID cleanup is needed. The token-pool contract fixes one request limit for
all jobs and rejects inconsistent limits. Final delivery has one exclusive
lease, so concurrent downloads may build products on ext4 while only one
process copies or renames into the delivery filesystem.

## CLI controls

Both streamers expose:

```text
--workers auto|N
--memory-limit-gib auto|N
--memory-reserve-gib auto|N
--global-request-limit N
--memory-high-watermark 0.80
--memory-critical-watermark 0.90
--resource-state-dir PATH
```

At the high watermark, new prefetch is suppressed and existing results drain.
At the critical watermark, the grid ledger is saved before the run exits;
point Parquet shards already committed atomically remain resumable.

## Web server

The Web task-count limit is only a ceiling. Each accepted plan carries an
estimated peak-memory reservation, and a queued task starts only when the sum
of active reservations fits the Web memory budget. Configure the server with:

```bash
export AEF_GRITS_WEB_MEMORY_GIB=16
export AEF_GRITS_WEB_MEMORY_RESERVE_GIB=2
export AEF_GRITS_WEB_GLOBAL_REQUESTS=8
export AEF_GRITS_RESOURCE_STATE=/home/user/aef_state/.resource_locks
```

For Linux NTFS delivery, `AEF_GRITS_WEB_STATE`, `AEF_GRITS_WEB_STAGING`, and
`AEF_GRITS_RESOURCE_STATE` must all remain on ext4/XFS. NTFS remains a final
delivery target only.
