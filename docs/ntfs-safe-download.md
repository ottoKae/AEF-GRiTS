# NTFS-safe download and recovery

Linux `ntfs3` is supported only as a final delivery target. AEF-GRiTS does not
claim to repair an NTFS kernel deadlock. Its storage protocol reduces exposure
and preserves enough native-filesystem state to stop safely and resume after an
administrator restores the mount.

## Storage roles

| Role | Recommended filesystem | Contents |
|---|---|---|
| State | ext4/XFS | checkpoint, catalog, report, PID, log, queue and incident record |
| Staging | ext4/XFS | one active point delivery or one active grid Zarr |
| Final | NTFS/NTFS3 allowed | immutable, completed Parquet/Zarr products only |

Grid fetch workers contact Earth Engine concurrently, but the coordinator is
the only Zarr writer. After every block is durable in staging, the checkpoint
is atomically replaced using `flush -> fsync -> os.replace -> parent fsync`.
Once a complete store is consolidated and marked, a separate process performs
a resumable, sequential copy to a signature-specific incoming directory on the
final volume. It renames that directory only after a successful copy. The
native checkpoint is then marked `committed`, the native catalog is updated,
and the staging copy may be removed.

Neither startup nor failure handling deletes an old NTFS temporary or incoming
path. A commit timeout writes `commit_incident.json`, requests termination only
once, and stops the task. A visible NTFS-related D-state process prevents new
work. Disk-space queries run in disposable processes so the Web server and
download coordinator cannot themselves enter uninterruptible I/O sleep.

## Command-line operation

```bash
STATE=/home/USER/aef_state/anhui_mgrs_2019
STAGE=/home/USER/aef_staging/anhui_mgrs_2019
FINAL=/mnt/hdda/USER/anhui_mgrs_2019

mkdir -p "$STATE" "$STAGE"

aef-grits-storage-check --final "$FINAL" --state "$STATE" --staging "$STAGE"

nohup aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 50RMU \
  --project YOUR_GEE_PROJECT \
  --years 2019 \
  --out-dir "$FINAL" \
  --state-dir "$STATE" \
  --staging-dir "$STAGE" \
  >"$STATE/download.log" 2>&1 &
printf '%s\n' "$!" >"$STATE/download.pid"
```

Do not redirect logs or store PID files under `/mnt/hdda` or `/mnt/hddb`.

## Web operation

```bash
export AEF_GRITS_WEB_OUTPUT=/mnt/hdda/USER/aef_web_output
export AEF_GRITS_WEB_STATE=/home/USER/aef_state/web
export AEF_GRITS_WEB_STAGING=/home/USER/aef_staging/web
python webapp/app.py
```

The Web runner passes a per-task state and staging directory to the same CLI.
Its signed plan, task manifest, structured events, full log, PID identity,
catalog, download report and validation report all remain under
`AEF_GRITS_WEB_STATE`.

## Administrator recovery after a stuck NTFS3 mount

Use an out-of-band console when SSH no longer reaches authentication. Do not
create more `rm`, `stat`, `tail`, GUI file-browser or recursive-scan processes
against the affected mount.

1. Reboot the host from the administrator console.
2. Verify that boot time changed and that no process is in D state.
3. Inspect `findmnt` and the kernel log before allowing writes.
4. If the NTFS volume is dirty or reports errors, unmount it and run the
   administrator-approved offline Windows `chkdsk`/Linux diagnostic workflow;
   never use the NTFS3 `force` mount option as a shortcut.
5. Mount the volume, perform one bounded health and free-space check, and stop
   if it does not return promptly.
6. Copy any legacy progress JSON to the ext4 state directory. Do not delete the
   original.
7. Resume only through the safe state/staging command above.

## Adopting a fully written legacy grid

If an old grid has every block recorded but lacks only catalog/report
finalization, import its ledger once and adopt it without re-downloading:

```bash
aef-grits-grid \
  --grid-scheme mgrs --tiles 50RMU \
  --project YOUR_GEE_PROJECT --years 2019 \
  --out-dir /mnt/hdda/USER/anhui_mgrs_2019 \
  --state-dir /home/USER/aef_state/anhui_mgrs_2019 \
  --staging-dir /home/USER/aef_staging/anhui_mgrs_2019 \
  --import-legacy-progress /mnt/hdda/USER/anhui_mgrs_2019/LEGACY.progress.json \
  --adopt-existing-complete
```

Adoption is refused unless the imported signature matches and the ledger count
equals the exact requested block count. The legacy file is never removed.
