"""Isolated validator/finalizer for a fully written legacy Zarr grid."""

from __future__ import annotations

import argparse
from pathlib import Path

import zarr

from .native_grid import NATIVE_PROTOCOL, manifest_signature


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--shape", nargs=4, required=True, type=int)
    parser.add_argument("--native-source-signature")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    group = zarr.open_group(
        str(args.path), mode="r" if args.validate_only else "r+", zarr_format=3, use_consolidated=False
    )
    if group.attrs.get("aef:signature") != args.signature:
        raise ValueError("Legacy Zarr signature differs")
    if tuple(group["embeddings"].shape) != tuple(args.shape):
        raise ValueError("Legacy Zarr shape differs")
    if args.native_source_signature is not None:
        manifest = group.attrs.get("aef:native_source_manifest", {})
        payload = {key: value for key, value in manifest.items() if key != "signature"}
        if (group.attrs.get("aef:native_protocol") != NATIVE_PROTOCOL
                or group.attrs.get("aef:native_source_signature") != args.native_source_signature
                or manifest.get("protocol") != NATIVE_PROTOCOL
                or manifest.get("signature") != args.native_source_signature
                or manifest_signature(payload) != args.native_source_signature):
            raise ValueError("Existing store lacks verified native-source provenance")
    if not args.validate_only:
        zarr.consolidate_metadata(str(args.path))


if __name__ == "__main__":
    main()
