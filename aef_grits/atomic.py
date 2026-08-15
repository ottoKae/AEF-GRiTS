"""Crash-safe control-file writers used by resumable download commands.

These helpers are intentionally for the *control plane* (state, ledgers,
catalogs and reports).  Callers should place those files on a native local
filesystem such as ext4/XFS/APFS, not on a Linux ``ntfs3`` mount.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable
import uuid

import pandas as pd


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the platform supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_after_fsync(temporary: Path, target: Path) -> None:
    with temporary.open("r+b") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    _fsync_directory(target.parent)


def _atomic_write(path: str | Path, writer: Callable[[Path], None]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:12]}"
    )
    if temporary.exists():
        raise FileExistsError(
            f"Unique control-file temporary already exists: {temporary}"
        )
    writer(temporary)
    _replace_after_fsync(temporary, target)


def write_json(payload: dict, path: str | Path) -> None:
    def writer(temporary: Path) -> None:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(path, writer)


def write_text(value: str, path: str | Path, *, encoding: str = "utf-8") -> None:
    def writer(temporary: Path) -> None:
        with temporary.open("w", encoding=encoding) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(path, writer)


def write_parquet(frame: pd.DataFrame, path: str | Path) -> None:
    _atomic_write(path, lambda temporary: frame.to_parquet(temporary, index=False))
