"""Filesystem-aware safety policy for long-running AEF downloads.

The module deliberately discovers Linux mounts from ``/proc/self/mountinfo``
instead of probing a possibly wedged output mount with ``stat(2)``.  It keeps
high-frequency state on a native filesystem and treats Linux NTFS/NTFS3 as a
final delivery target only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Iterable

from aef_grits.atomic import write_json
from aef_grits.resource_control import FileLease, default_resource_root


NTFS_TYPES = frozenset({"ntfs", "ntfs3", "fuseblk"})
NATIVE_STAGING_TYPES = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "apfs"})


def terminate_process_group_once(process: subprocess.Popen) -> None:
    """Request one bounded shutdown of an isolated worker process group.

    The caller deliberately does not wait indefinitely: a worker blocked in
    uninterruptible kernel I/O cannot be reaped safely by retrying signals.
    """
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    # Reap a normally terminating worker without ever waiting indefinitely for
    # kernel I/O.  A genuine D-state process remains recorded for operators.
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.025)


@dataclass(frozen=True)
class MountInfo:
    mount_point: str
    fs_type: str
    mount_options: str = ""
    source: str = ""

    @property
    def is_linux_ntfs(self) -> bool:
        return self.fs_type.lower() in NTFS_TYPES


@dataclass(frozen=True)
class StorageLayout:
    final_root: str
    state_root: str
    staging_root: str | None
    final_mount: MountInfo | None
    state_mount: MountInfo | None
    staging_mount: MountInfo | None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["final_is_linux_ntfs"] = bool(
            self.final_mount and self.final_mount.is_linux_ntfs
        )
        return payload


def _unescape_mount_field(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value
    )


def parse_mountinfo(text: str) -> list[MountInfo]:
    mounts: list[MountInfo] = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 2:
            continue
        mounts.append(
            MountInfo(
                mount_point=_unescape_mount_field(left_fields[4]),
                fs_type=right_fields[0].lower(),
                mount_options=left_fields[5],
                source=_unescape_mount_field(right_fields[1]),
            )
        )
    return mounts


def linux_mounts(path: str | Path = "/proc/self/mountinfo") -> list[MountInfo]:
    if os.name != "posix" or not Path(path).is_file():
        return []
    return parse_mountinfo(Path(path).read_text(encoding="utf-8", errors="replace"))


def mount_for_path(
    path: str | Path, mounts: Iterable[MountInfo] | None = None
) -> MountInfo | None:
    """Return the longest lexical mount match without touching ``path``."""
    absolute = os.path.abspath(os.path.expanduser(os.fspath(path)))
    candidates = []
    for mount in list(mounts) if mounts is not None else linux_mounts():
        point = os.path.abspath(mount.mount_point)
        try:
            if os.path.commonpath([absolute, point]) == point:
                candidates.append(mount)
        except ValueError:
            continue
    return max(candidates, key=lambda item: len(item.mount_point), default=None)


def ntfs_d_state_processes(
    mount_points: Iterable[str], proc_root: str | Path = "/proc"
) -> list[dict]:
    """Find D-state processes visibly associated with an NTFS mount.

    This reads procfs only.  It never queries the suspect filesystem.  Kernel
    threads without a useful command line are reported when their wait channel
    itself names NTFS.
    """
    root = Path(proc_root)
    if (os.name != "posix" and os.fspath(proc_root) == "/proc") or not root.is_dir():
        return []
    needles = [os.path.abspath(value) for value in mount_points]
    records: list[dict] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            close = stat.rfind(")")
            state = stat[close + 2 :].split(maxsplit=1)[0] if close >= 0 else ""
            if state != "D":
                continue
            wchan = (entry / "wchan").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
            comm = (entry / "comm").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        visible = " ".join((wchan, command, comm)).lower()
        if "ntfs" in visible or any(value in command for value in needles):
            records.append(
                {
                    "pid": int(entry.name),
                    "state": state,
                    "wchan": wchan,
                    "comm": comm,
                    "command": command,
                }
            )
    return records


def validate_storage_layout(
    final_root: str | Path,
    state_root: str | Path,
    staging_root: str | Path | None,
    *,
    create_native_dirs: bool = True,
    mounts: Iterable[MountInfo] | None = None,
) -> StorageLayout:
    """Validate that an NTFS final target has native state and staging roots."""
    mount_list = list(mounts) if mounts is not None else linux_mounts()
    final = os.path.abspath(os.path.expanduser(os.fspath(final_root)))
    state = os.path.abspath(os.path.expanduser(os.fspath(state_root)))
    staging = (
        os.path.abspath(os.path.expanduser(os.fspath(staging_root)))
        if staging_root is not None
        else None
    )
    final_mount = mount_for_path(final, mount_list)
    state_mount = mount_for_path(state, mount_list)
    staging_mount = mount_for_path(staging, mount_list) if staging else None
    if final_mount and final_mount.is_linux_ntfs:
        if state_mount is None or state_mount.fs_type not in NATIVE_STAGING_TYPES:
            raise ValueError(
                "Linux NTFS output requires --state-dir on ext4/XFS or another "
                "native local filesystem"
            )
        if (
            staging is None
            or staging_mount is None
            or staging_mount.fs_type not in NATIVE_STAGING_TYPES
        ):
            raise ValueError(
                "Linux NTFS output requires --staging-dir on ext4/XFS or another "
                "native local filesystem"
            )
        related = ntfs_d_state_processes(
            [mount.mount_point for mount in mount_list if mount.is_linux_ntfs]
        )
        if related:
            pids = ", ".join(str(record["pid"]) for record in related[:8])
            raise RuntimeError(
                f"Refusing new NTFS writes: related D-state processes detected "
                f"on {final_mount.mount_point} (PIDs {pids})"
            )
    if create_native_dirs:
        Path(state).mkdir(parents=True, exist_ok=True)
        if staging:
            Path(staging).mkdir(parents=True, exist_ok=True)
    return StorageLayout(
        final_root=final,
        state_root=state,
        staging_root=staging,
        final_mount=final_mount,
        state_mount=state_mount,
        staging_mount=staging_mount,
    )


def isolated_disk_free(
    path: str | Path,
    *,
    timeout_seconds: float = 10.0,
    incident_path: str | Path | None = None,
) -> int:
    """Query free bytes in a disposable process so the caller cannot enter D state."""
    command = [
        sys.executable,
        "-c",
        (
            "import shutil,sys; from pathlib import Path; p=Path(sys.argv[1]); "
            "p=next((x for x in (p,*p.parents) if x.exists()),None); "
            "assert p is not None; print(shutil.disk_usage(p).free)"
        ),
        os.fspath(path),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    started = time.monotonic()
    while process.poll() is None:
        if time.monotonic() - started > timeout_seconds:
            if incident_path is not None:
                write_json(
                    {
                        "status": "disk_probe_timeout",
                        "pid": process.pid,
                        "path": os.fspath(path),
                        "timeout_seconds": timeout_seconds,
                        "action": "Stop new work and inspect the filesystem; do not repeat the probe.",
                    },
                    incident_path,
                )
            terminate_process_group_once(process)
            raise TimeoutError(
                f"Disk-space probe timed out for {path}; no new write should start"
            )
        time.sleep(0.05)
    stdout, stderr = process.communicate()
    if process.returncode:
        raise RuntimeError(f"Disk-space probe failed for {path}: {stderr.strip()}")
    return int(stdout.strip())


def _run_commit_process(
    source: Path,
    destination: Path,
    *,
    signature: str,
    timeout_seconds: float,
    incident_path: Path,
) -> None:
    command = [
        sys.executable,
        "-m",
        "aef_grits.transfer_worker",
        os.fspath(source),
        os.fspath(destination),
        "--signature",
        signature,
    ]
    process = subprocess.Popen(command, start_new_session=os.name == "posix")
    started = time.monotonic()
    while process.poll() is None:
        if time.monotonic() - started > timeout_seconds:
            write_json(
                {
                    "status": "copy_timeout",
                    "pid": process.pid,
                    "source": str(source),
                    "destination": str(destination),
                    "timeout_seconds": timeout_seconds,
                    "action": "Do not retry, delete, or kill repeatedly. Check the final filesystem first.",
                },
                incident_path,
            )
            terminate_process_group_once(process)
            raise TimeoutError(
                f"Final-store copy exceeded {timeout_seconds:g}s; incident recorded "
                f"at {incident_path}. No cleanup was attempted."
            )
        time.sleep(0.25)
    if process.returncode:
        raise RuntimeError(f"Final-store copy exited with code {process.returncode}")


def commit_staged_tree(
    source: str | Path,
    destination: str | Path,
    *,
    signature: str,
    state_dir: str | Path,
    resource_root: str | Path | None = None,
    timeout_seconds: float = 21_600,
) -> Path:
    """Copy one complete staged tree through a resumable incoming directory.

    No destination cleanup is automatic.  A failed or interrupted copy leaves
    the incoming tree in place so the next healthy run can resume it.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    state_path = Path(state_dir)
    incident = state_path / "commit_incident.json"
    lock_root = Path(resource_root) if resource_root is not None else default_resource_root(state_path)
    lease = FileLease(lock_root / "final-delivery.lock")
    lease.acquire(timeout_seconds=timeout_seconds)
    try:
        _run_commit_process(
            source_path,
            destination_path,
            signature=signature,
            timeout_seconds=timeout_seconds,
            incident_path=incident,
        )
    finally:
        lease.release()
    return destination_path


def adopt_existing_store(
    path: str | Path,
    *,
    signature: str,
    expected_shape: tuple[int, int, int, int],
    state_dir: str | Path,
    timeout_seconds: float = 600.0,
) -> None:
    """Validate/consolidate one legacy final store in an isolated process."""
    state_path = Path(state_dir)
    incident = state_path / "adoption_incident.json"
    command = [
        sys.executable,
        "-m",
        "aef_grits.adopt_worker",
        os.fspath(path),
        "--signature",
        signature,
        "--shape",
        *(str(value) for value in expected_shape),
    ]
    process = subprocess.Popen(command, start_new_session=os.name == "posix")
    started = time.monotonic()
    while process.poll() is None:
        if time.monotonic() - started > timeout_seconds:
            write_json(
                {
                    "status": "adoption_timeout",
                    "pid": process.pid,
                    "path": os.fspath(path),
                    "timeout_seconds": timeout_seconds,
                    "action": "Stop new work and inspect the final filesystem; no cleanup was attempted.",
                },
                incident,
            )
            terminate_process_group_once(process)
            raise TimeoutError(f"Legacy-store adoption timed out for {path}")
        time.sleep(0.1)
    if process.returncode:
        raise RuntimeError(f"Legacy-store adoption exited with code {process.returncode}")
