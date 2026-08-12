"""Paths and validation for data resources distributed with AEF-GRiTS."""

from __future__ import annotations

import os
from pathlib import Path


PACKAGE_DATA = Path(__file__).resolve().parent / "data"
BUILTIN_MGRS_INDEX = PACKAGE_DATA / "mgrs.parquet"


def mgrs_index_path(explicit: str | Path | None = None) -> Path:
    """Resolve the MGRS index without depending on another source repository.

    Resolution order is an explicit argument, the optional
    ``AEF_GRITS_MGRS_INDEX`` environment variable, and finally the global index
    shipped inside this Python package. Custom paths are supported for advanced
    workflows, but normal installations always use the packaged table.
    """
    configured = explicit or os.environ.get("AEF_GRITS_MGRS_INDEX")
    path = Path(configured).expanduser() if configured else BUILTIN_MGRS_INDEX
    if not path.is_file():
        source = "custom" if configured else "packaged"
        raise FileNotFoundError(f"AEF-GRiTS {source} MGRS index is missing: {path}")
    return path.resolve()
