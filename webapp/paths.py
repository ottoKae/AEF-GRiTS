"""Writable defaults independent of the source tree or installed package."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def default_state_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "AEF-GRiTS"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AEF-GRiTS"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "aef-grits"


def default_output_root() -> Path:
    return Path.home() / "Downloads" / "AEF-GRiTS"
