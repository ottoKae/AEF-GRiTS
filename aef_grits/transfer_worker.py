"""Single-writer fallback used when rsync is unavailable."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess

from aef_grits.atomic import _fsync_directory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--signature", required=True)
    args = parser.parse_args()
    incoming = args.destination.with_name(
        f".{args.destination.name}.incoming-{args.signature[:12]}"
    )
    if args.destination.exists():
        raise FileExistsError(
            f"Final destination already exists: {args.destination}; no cleanup attempted"
        )
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    incoming.mkdir(parents=True, exist_ok=True)
    rsync = shutil.which("rsync")
    if rsync:
        subprocess.run(
            [rsync, "-a", "--partial", f"{args.source}{os.sep}", f"{incoming}{os.sep}"],
            check=True,
        )
    else:
        shutil.copytree(
            args.source,
            incoming,
            dirs_exist_ok=True,
            copy_function=shutil.copy2,
        )
    os.replace(incoming, args.destination)
    _fsync_directory(args.destination.parent)


if __name__ == "__main__":
    main()
