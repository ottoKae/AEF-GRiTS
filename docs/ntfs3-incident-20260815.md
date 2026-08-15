# NTFS3 incident remediation status — 2026-08-15

## Incident state supplied by the operator

- Host: `202.114.121.151`
- Affected mounts: `/mnt/hdda` and `/mnt/hddb`, both Linux NTFS3
- Active AEF process: stopped
- Saved output: about 23 GB
- Completed reports: three grids
- `50RMU`: 1,849/1,849 blocks written; final catalog/report still pending
- Failure mode: multiple processes in uninterruptible D state during NTFS
  metadata operations; signals including SIGKILL cannot complete kernel I/O

## Remote access result

From the authorized client, TCP port 22 accepts a connection, but the server
closes it before sending an SSH protocol banner. Five bounded retries plus
later single retries produced the same result. Therefore no remote command,
mount access, reboot or source-directory query was performed. Recovery now
requires the administrator/out-of-band console; ordinary SSH cannot initiate
the reboot in the current state.

## Implemented repository remediation

- Linux mount discovery reads `/proc/self/mountinfo` without probing the target.
- NTFS/NTFS3 output requires native `--state-dir` and `--staging-dir`.
- Checkpoints, catalogs, reports, PID/log/task state remain off NTFS.
- Grid fetchers may be concurrent; all Zarr writes remain in one coordinator.
- Each grid is completed on native staging before an isolated single-writer
  process copies it into a signature-specific NTFS incoming directory.
- The final rename occurs only after the copy succeeds; no `--delete` behavior
  is used.
- Commit and disk-space probes run in disposable processes with bounded time.
  A timeout records an incident on native storage and does not clean NTFS.
- Startup detects visibly related D-state processes through procfs and refuses
  new NTFS writes.
- Control writes use flush, fsync, atomic replace and parent-directory fsync.
- Web cancellation does not send signals to a process tree containing a
  D-state member.
- A legacy completed-grid ledger can be imported and adopted without deleting
  the source or re-downloading all blocks.

## Verification

`69 passed` on Windows with Python 3.12. The suite includes a synthetic
end-to-end grid transfer, external committed ledger, atomic control files,
stale-temporary preservation, D-state detection, NTFS layout rejection,
isolated disk query, non-destructive duplicate destination handling, and all
pre-existing point/grid/Web tests.

## Remaining server actions

1. Reboot through the administrator console.
2. Verify changed boot time, zero D-state processes, mounts and NTFS3 kernel log.
3. Perform the approved offline filesystem check if either NTFS volume is dirty.
4. Verify ext4 capacity for one staged `50RMU` product and NTFS capacity for the
   complete delivery.
5. Deploy this branch to the user environment.
6. Import the existing `50RMU` progress JSON into ext4 state and run the strict
   completed-store adoption path.
7. Validate the Zarr signature/shape/compression and rebuild catalog/report.
8. Resume remaining grids only through native state/staging.
