"""Isolated validator/finalizer for a fully written legacy Zarr grid."""

from __future__ import annotations

import argparse
from pathlib import Path

import zarr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--shape", nargs=4, required=True, type=int)
    args = parser.parse_args()
    group = zarr.open_group(
        str(args.path), mode="r+", zarr_format=3, use_consolidated=False
    )
    if group.attrs.get("aef:signature") != args.signature:
        raise ValueError("Legacy Zarr signature differs")
    if tuple(group["embeddings"].shape) != tuple(args.shape):
        raise ValueError("Legacy Zarr shape differs")
    zarr.consolidate_metadata(str(args.path))


if __name__ == "__main__":
    main()
