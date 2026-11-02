"""Native local folder selection, isolated from Flask's worker threads."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def pick_directory(initial_dir: Path, timeout_seconds: float = 120) -> str | None:
    """Return the user's selection, None on cancel, or an actionable error."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "webapp.folder_picker", "--initial-dir", str(initial_dir)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout_seconds,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Folder selection timed out; retry or use the web folder browser") from exc
    except OSError as exc:
        raise RuntimeError("Could not start the local folder chooser; use the web folder browser") from exc
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("Expected a folder result")
        if result.returncode:
            raise RuntimeError(str(payload.get("error", "The native folder chooser failed")))
        selected = payload["path"]
        if selected is not None and (not isinstance(selected, str) or not selected):
            raise ValueError("Expected a directory path or cancellation")
    except (ValueError, KeyError) as exc:
        raise RuntimeError("Could not read the folder selection; use the web folder browser") from exc
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print(json.dumps({"error": "Tkinter is unavailable in this Python environment; install Tk or use the web folder browser"}))
        return 1

    root = None
    try:
        # Tk requires the main thread; this module runs in its own short-lived process.
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root, initialdir=str(args.initial_dir), mustexist=True,
            title="选择 AEF 数据存储文件夹",
        )
        print(json.dumps({"path": str(Path(selected).resolve()) if selected else None}))
        return 0
    except tk.TclError:
        print(json.dumps({"error": "The native folder chooser needs a local desktop session; use the web folder browser instead"}))
        return 1
    finally:
        if root is not None:
            root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
