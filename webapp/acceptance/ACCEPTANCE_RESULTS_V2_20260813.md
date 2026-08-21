# Web workflow reliability acceptance — 2026-08-13 v2

Project: `<redacted-project>`<br>
Year: `2025`<br>
Environment: `py312_torch`<br>
Server: `http://127.0.0.1:5555`

This acceptance run uses the final two-stage planning protocol. Point input is
uploaded and validated during preflight, while dense-grid execution is allowed
only from a signed, expiring, one-use plan. All executed workflows used real
Earth Engine requests; the full MGRS download was deliberately not confirmed.

## Real executions

| Workflow | Run ID | Planned/validated product | Wall time | Structured events | Result |
|---|---|---|---:|---:|---|
| Point CSV | `20260813_164142_9313e6` | 1 sample, 1 Parquet shard, 64/64 features, no incomplete rows | 6 s | 3 | PASS |
| Reference | `20260813_164142_397d79` | `acceptance_v2_ref_16x16`, `[1,64,16,16]`, EPSG:32717, 10 m | 8 s | 4 | PASS |
| Tessera | `20260813_164142_ab5bd4` | `grid_-80.05_-1.05`, `[1,64,1107,1114]`, EPSG:32717, 10 m | 209 s | 28 | PASS |

Every executed task passed the automatic post-run validator with no errors or
warnings. Both dense outputs report lossless `Zstd-7`, `no-shuffle` compression.
The acceptance-v2 directory contains 46 files totalling 70,926,051 bytes.

Each output directory contains `web_task_report.json`, including the signed
plan, exact command, validation report, Git revision and dirty state, Python and
package versions, AEF dataset/year/dimension metadata, and the checksum of the
packaged authoritative MGRS index. The associated task directories contain
atomic `task.json`, full `run.log`, and append-only `events.jsonl` records.

## MGRS safety and CRS acceptance

The Anhui 0.08-degree AOI resolved to authoritative grids `50RMV` and `50RNV`.
The plan reported:

- AOI exchange CRS: EPSG:4326;
- output CRS: EPSG:32650;
- resolution: 10 m;
- estimated uncompressed size: 57.4877 GiB;
- required free space including reserve: 59.4877 GiB;
- observed free space: 1787.8842 GiB;
- required phrase: `DOWNLOAD 57.49 GiB`.

The phrase was not entered, so no MGRS download task was created. The returned
WGS84 GeoJSON footprints use the authoritative Parquet geometry rather than
guessing a boundary from the MGRS identifier. Reference-grid projected bounds
were likewise transformed to WGS84 for browser display while the output stayed
in its native EPSG:32717 grid.

## Automated acceptance

`python -m pytest -q` completed with **54 passed**. The suite includes four
Playwright browser workflows:

1. point-file preflight followed by confirmed execution;
2. Tessera planning, CRS/footprint display, and execution;
3. authoritative MGRS planning and cancellation at strong confirmation;
4. Reference planning and execution.

The API suite also covers queue saturation (HTTP 429), plan replay rejection,
disk and raw-size blocking, exact confirmation phrases, process-tree
cancellation, restart orphan recovery, structured progress parsing, WGS84 AOI
validation, authoritative grid footprints, automatic product validation, and
the results/provenance endpoint.

The browser screenshot [completed_tasks_v2.png](completed_tasks_v2.png) records
the three validated real tasks, their output paths, progress, and validation
status. The browser keeps only the latest 1,000 displayed log lines; full logs
remain downloadable from each card.

## Acceptance conclusion

The ten requested reliability and interface items are implemented and accepted
for trusted localhost use. No production-size MGRS job was launched. Public or
multi-user deployment remains out of scope because the application deliberately
uses local Earth Engine credentials and server-side output paths.
