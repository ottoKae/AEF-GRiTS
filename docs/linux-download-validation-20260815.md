# Linux download reliability validation — 2026-08-15

## Scope

This validation was run from a clean Ubuntu 24.04 WSL2 installation on a
native ext4 filesystem.  The repository, Conda environment, state directory,
staging directory and test outputs were all placed on ext4; `/mnt/d` was used
only as the read-only source for synchronizing the code under test.

Environment:

- Linux kernel: `6.6.87.2-microsoft-standard-WSL2`
- Python: 3.12.13
- Zarr: 3.3.0
- Earth Engine API: 1.7.39
- Test environment: `aef_grits_linux_test`

## Results

- Linux test suite: **64 passed**, including the POSIX-only process-group
  timeout test.  Six browser/Windows-specific cases are not collected on
  Linux.  The complete cross-platform suite is **69 passed and 1 POSIX-only
  test skipped on Windows**.
- The environment doctor passed Python, required packages, CRS conversion,
  packaged MGRS index and a real lossless Zarr v3 Zstd-7/no-shuffle round trip.
- The storage checker identified final, state and staging as ext4 and completed
  all isolated capacity probes successfully.
- A real synthetic grid ran through Zarr creation, chunk writes, metadata
  consolidation, ext4 staging, isolated single-writer delivery, external
  catalog/report update and completed-store reconciliation.
- A forced hanging `rsync` was stopped at the configured deadline by terminating
  its isolated POSIX process group.  The command returned in under three
  seconds, wrote `commit_incident.json`, and left no transfer worker or child
  process running.
- A 2025 MGRS `50RMU` plan resolved successfully from the packaged authoritative
  MGRS index: EPSG:32650, 10,980 x 10,980 pixels, 64 float32 bands and 1,849
  computePixels blocks under the production request layout.

The online Earth Engine call was intentionally not run in WSL because no Earth
Engine credential was installed in that clean Linux account.  Authentication
is the only skipped preflight item; the API package and every downstream write,
resume and delivery path were exercised.

## Additional safeguards verified in this pass

- Timeout handling covers the complete isolated process group, preventing an
  `rsync` child from surviving its Python worker.
- The main Web process does not create, resolve, scan or browse a Linux NTFS
  output directory.  NTFS directory creation is deferred to the isolated
  delivery worker.
- Web product validation runs in its own POSIX process group with a bounded
  deadline.
- The environment doctor uses an isolated capacity probe for Linux NTFS rather
  than opening a temporary file on the suspect filesystem.
- Related D-state processes are discovered from procfs and block all new NTFS
  commits.  No automatic NTFS cleanup, recursive scan, repeated signal or
  `kill -9` is issued.

## Production acceptance still required

The application-side pipeline is suitable for deployment with native ext4/XFS
state and staging plus isolated final delivery.  It cannot repair an NTFS3
kernel deadlock that already exists.  Production acceptance still requires the
server to boot cleanly, expose SSH, show no related D-state processes, and pass
one authenticated small Earth Engine download before the saved 23 GB job is
adopted or resumed.

