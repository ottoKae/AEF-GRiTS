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
--request-timeout-seconds 300
--resource-profile workstation-auto|low-memory-1g|server-8g|server-16g
```

At the high watermark, new prefetch is suppressed and existing results drain.
At the critical watermark, the grid ledger is saved before the run exits;
point Parquet shards already committed atomically remain resumable.

Earth Engine's hidden retry loop is disabled. AEF-GRiTS applies the configured
client deadline to every API attempt, classifies timeout/transient/permanent
errors, and emits auditable retry events. A retryable terminal failure exits
with status 75 after preserving the grid ledger or committed point shards.

Point-source preflight is streaming. CSV and Parquet use native batches;
supported vectors use bounded feature batches, and polygon interior pixels are
emitted incrementally. Exact duplicate checks use a SQLite audit on the native
state filesystem. Point results can be reopened as a PyArrow Dataset through
`open_aef_point_dataset()` and projected or filtered batchwise.

## Web server

The Web task-count limit is only a ceiling. Each accepted plan carries an
estimated peak-memory reservation. Admission jointly enforces memory and active
task limits. Small jobs may backfill temporarily when the oldest job does not
fit, while an aging limit prevents starvation. Configure the server with:

```bash
export AEF_GRITS_WEB_MEMORY_GIB=16
export AEF_GRITS_WEB_MEMORY_RESERVE_GIB=2
export AEF_GRITS_WEB_GLOBAL_REQUESTS=8
export AEF_GRITS_WEB_RESOURCE_PROFILE=server-16g
export AEF_GRITS_WEB_MAX_BYPASS_SECONDS=30
export AEF_GRITS_RESOURCE_STATE=/home/user/aef_state/.resource_locks
```

For Linux NTFS delivery, `AEF_GRITS_WEB_STATE`, `AEF_GRITS_WEB_STAGING`, and
`AEF_GRITS_RESOURCE_STATE` must all remain on ext4/XFS. NTFS remains a final
delivery target only.

Reports include request latency percentiles, retry/timeout counts, estimated
deserialized response bytes, token-wait time, Zarr/Parquet write time and
final-delivery time. Combine
explicit reports with `aef-grits-benchmark-summary`; never recursively scan an
unhealthy NTFS mount for reports.
