from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pandas as pd
import pytest
import aef_grits.storage_safety as storage_safety

from aef_grits.atomic import write_json, write_parquet
from aef_grits.storage_safety import (
    MountInfo,
    commit_staged_tree,
    isolated_disk_free,
    mount_for_path,
    ntfs_d_state_processes,
    parse_mountinfo,
    validate_storage_layout,
)


def test_mountinfo_parser_and_longest_lexical_match():
    mounts = parse_mountinfo(
        "36 25 8:1 / / rw,relatime - ext4 /dev/nvme0n1p2 rw\n"
        "44 36 8:2 / /mnt/hdda rw,relatime - ntfs3 /dev/sda2 rw,prealloc\n"
    )
    assert mount_for_path("/mnt/hdda/project/a.zarr", mounts).fs_type == "ntfs3"
    assert mount_for_path("/home/user/state", mounts).fs_type == "ext4"


def test_linux_ntfs_requires_native_state_and_staging(tmp_path):
    final = tmp_path / "final"
    state = tmp_path / "state"
    staging = tmp_path / "staging"
    mounts = [
        MountInfo(str(tmp_path), "ext4"),
        MountInfo(str(final), "ntfs3"),
    ]
    with pytest.raises(ValueError, match="state-dir"):
        validate_storage_layout(final, final / "state", staging, mounts=mounts)
    with pytest.raises(ValueError, match="staging-dir"):
        validate_storage_layout(final, state, None, mounts=mounts)
    layout = validate_storage_layout(final, state, staging, mounts=mounts)
    assert layout.final_mount.is_linux_ntfs
    assert Path(layout.state_root).is_dir()
    assert Path(layout.staging_root).is_dir()


def test_atomic_control_writers_leave_no_temporary(tmp_path):
    target = tmp_path / "state.json"
    write_json({"complete": 1}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"complete": 1}
    parquet = tmp_path / "catalog.parquet"
    write_parquet(pd.DataFrame({"grid_id": ["A"]}), parquet)
    assert pd.read_parquet(parquet).grid_id.tolist() == ["A"]
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_atomic_writer_preserves_stale_temporary_without_blocking(tmp_path):
    target = tmp_path / "state.json"
    stale = tmp_path / f".{target.name}.tmp.{__import__('os').getpid()}"
    stale.write_text("do not delete", encoding="utf-8")
    write_json({"new": True}, target)
    assert stale.read_text(encoding="utf-8") == "do not delete"
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_staged_tree_commit_is_single_and_non_destructive(tmp_path):
    source = tmp_path / "stage" / "tile.zarr"
    source.mkdir(parents=True)
    (source / "zarr.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "final" / "tile.zarr"
    state = tmp_path / "state"
    state.mkdir()
    commit_staged_tree(
        source,
        destination,
        signature="abcdef1234567890",
        state_dir=state,
        timeout_seconds=30,
    )
    assert (destination / "zarr.json").read_text(encoding="utf-8") == "{}"
    with pytest.raises(RuntimeError, match="exited with code"):
        commit_staged_tree(
            source,
            destination,
            signature="abcdef1234567890",
            state_dir=state,
            timeout_seconds=30,
        )
    assert (destination / "zarr.json").is_file()


def test_disk_free_probe_is_isolated(tmp_path):
    assert isolated_disk_free(tmp_path, timeout_seconds=10) > 0


def test_d_state_detection_reads_procfs_without_touching_mount(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    (process / "stat").write_text("123 (rm) D 1 2 3", encoding="utf-8")
    (process / "wchan").write_text("ntfs_inode_write", encoding="utf-8")
    (process / "cmdline").write_bytes(b"rm\0/mnt/hdda/stale.tmp\0")
    (process / "comm").write_text("rm\n", encoding="utf-8")
    records = ntfs_d_state_processes(["/mnt/hdda"], proc_root=tmp_path)
    assert records[0]["pid"] == 123


def test_ntfs_layout_refuses_visible_d_state(tmp_path, monkeypatch):
    final = tmp_path / "final"
    state = tmp_path / "state"
    staging = tmp_path / "staging"
    mounts = [
        MountInfo(str(tmp_path), "ext4"),
        MountInfo(str(final), "ntfs3"),
    ]
    monkeypatch.setattr(
        storage_safety,
        "ntfs_d_state_processes",
        lambda *args, **kwargs: [{"pid": 77}],
    )
    with pytest.raises(RuntimeError, match="D-state"):
        validate_storage_layout(final, state, staging, mounts=mounts)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression test")
def test_commit_timeout_terminates_the_isolated_process_group(tmp_path, monkeypatch):
    source = tmp_path / "stage" / "tile.zarr"
    source.mkdir(parents=True)
    (source / "zarr.json").write_text("{}", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rsync = fake_bin / "rsync"
    fake_rsync.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    fake_rsync.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="incident recorded"):
        commit_staged_tree(
            source,
            tmp_path / "final" / "tile.zarr",
            signature="timeout123456789",
            state_dir=state,
            timeout_seconds=0.5,
        )
    assert time.monotonic() - started < 3.0
    incident = json.loads((state / "commit_incident.json").read_text(encoding="utf-8"))
    assert incident["status"] == "copy_timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(int(incident["pid"]), 0)
