"""Cross-platform preflight checks for AEF-GRiTS download environments."""

from __future__ import annotations

import argparse
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
from typing import Any

from aef_grits.storage_safety import isolated_disk_free, mount_for_path


PACKAGE_GROUPS = {
    "point": (
        ("numpy", "numpy"),
        ("pandas", "pandas"),
        ("pyarrow", "pyarrow"),
        ("ee", "earthengine-api"),
        ("google.auth", "google-auth"),
        ("requests", "requests"),
    ),
    "grid": (
        ("affine", "affine"),
        ("geopandas", "geopandas"),
        ("mgrs", "mgrs"),
        ("pyproj", "pyproj"),
        ("rasterio", "rasterio"),
        ("shapely", "shapely"),
        ("xarray", "xarray"),
        ("zarr", "zarr"),
    ),
    "web": (
        ("flask", "flask"),
        ("psutil", "psutil"),
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("point", "grid", "web", "all"), default="all")
    parser.add_argument("--project", help="Earth Engine quota project to initialize")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "outputs",
        help="Planned output root used for write and free-space checks",
    )
    parser.add_argument("--minimum-free-gib", type=float, default=2.0)
    parser.add_argument("--skip-ee", action="store_true", help="Skip online Earth Engine initialization")
    parser.add_argument("--json", action="store_true", help="Print only the JSON report")
    return parser.parse_args(argv)


def _record(checks: list[dict[str, Any]], name: str, status: str, detail: str) -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def _credential_candidates() -> list[Path]:
    candidates = [Path.home() / ".config" / "earthengine" / "credentials"]
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.home() / ".config" / "gcloud" / "application_default_credentials.json")
    return list(dict.fromkeys(path.resolve() for path in candidates))


def run_checks(
    *,
    mode: str,
    project: str | None,
    output: Path,
    minimum_free_gib: float,
    skip_ee: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if sys.version_info >= (3, 10):
        _record(checks, "python", "pass", sys.version.split()[0])
    else:
        _record(checks, "python", "fail", f"Python {sys.version.split()[0]}; require >=3.10")

    packages = list(PACKAGE_GROUPS["point"])
    if mode in {"grid", "web", "all"}:
        packages.extend(PACKAGE_GROUPS["grid"])
    if mode in {"web", "all"}:
        packages.extend(PACKAGE_GROUPS["web"])
    seen: set[str] = set()
    for module_name, distribution in packages:
        if module_name in seen:
            continue
        seen.add(module_name)
        try:
            import_module(module_name)
            try:
                installed = version(distribution)
            except PackageNotFoundError:
                installed = "importable"
            _record(checks, f"package:{distribution}", "pass", installed)
        except Exception as exc:
            _record(checks, f"package:{distribution}", "fail", str(exc))

    output = output.expanduser().absolute()
    try:
        output_mount = mount_for_path(output)
        if output_mount and output_mount.is_linux_ntfs:
            free_gib = isolated_disk_free(output, timeout_seconds=10) / 1024**3
            status = "warn" if free_gib >= minimum_free_gib else "fail"
            detail = (
                f"Linux NTFS capacity reachable in isolated probe; free={free_gib:.2f} GiB; "
                f"required>={minimum_free_gib:.2f} GiB. Use native --state-dir and "
                "--staging-dir; final writes are performed by an isolated worker."
            )
        else:
            output.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix=".aef-grits-write-", dir=output, delete=True
            ) as stream:
                stream.write(b"ok")
                stream.flush()
            usage = shutil.disk_usage(output)
            free_gib = usage.free / 1024**3
            status = "pass" if free_gib >= minimum_free_gib else "fail"
            detail = (
                f"writable={output}; free={free_gib:.2f} GiB; "
                f"required>={minimum_free_gib:.2f} GiB"
            )
        _record(
            checks,
            "output",
            status,
            detail,
        )
    except OSError as exc:
        _record(checks, "output", "fail", str(exc))

    if mode in {"grid", "web", "all"}:
        try:
            from pyproj import Transformer

            x, y = Transformer.from_crs("EPSG:4326", "EPSG:32650", always_xy=True).transform(117.0, 31.0)
            if not (100_000 < x < 900_000 and y > 0):
                raise RuntimeError(f"unexpected projected coordinate: {x}, {y}")
            _record(checks, "proj", "pass", f"EPSG:4326 -> EPSG:32650 = {x:.1f}, {y:.1f}")
        except Exception as exc:
            _record(checks, "proj", "fail", str(exc))

        try:
            import numpy as np
            import zarr
            from zarr.codecs import BloscCodec

            with tempfile.TemporaryDirectory(prefix="aef-grits-zarr-") as directory:
                root = zarr.open_group(directory, mode="w", zarr_format=3)
                array = root.create_array(
                    "aef",
                    shape=(1, 64, 8, 8),
                    chunks=(1, 64, 8, 8),
                    dtype="float32",
                    compressors=[BloscCodec(cname="zstd", clevel=7, shuffle="noshuffle")],
                )
                values = np.arange(array.size, dtype=np.float32).reshape(array.shape)
                array[:] = values
                if not np.array_equal(array[:], values):
                    raise RuntimeError("Zarr lossless round-trip mismatch")
            _record(checks, "zarr_v3", "pass", "float32 Zstd-7/no-shuffle round-trip")
        except Exception as exc:
            _record(checks, "zarr_v3", "fail", str(exc))

        try:
            import pandas as pd
            from .resources import BUILTIN_MGRS_INDEX

            required = {"mgrs_tile_id", "utm_epsg", "utm_wkt"}
            columns = set(pd.read_parquet(BUILTIN_MGRS_INDEX, columns=sorted(required)).columns)
            missing = sorted(required - columns)
            if missing:
                raise RuntimeError(f"missing columns: {missing}")
            _record(checks, "mgrs_index", "pass", str(BUILTIN_MGRS_INDEX))
        except Exception as exc:
            _record(checks, "mgrs_index", "fail", str(exc))

    credentials = [path for path in _credential_candidates() if path.exists()]
    if credentials:
        _record(checks, "credentials", "pass", ", ".join(map(str, credentials)))
    else:
        _record(checks, "credentials", "warn", "none found; run earthengine authenticate")

    if not skip_ee:
        if not project:
            _record(checks, "earth_engine", "warn", "not initialized; pass --project PROJECT_ID")
        else:
            try:
                from .earth_engine import initialize

                initialize(project)
                _record(checks, "earth_engine", "pass", f"initialized with project={project}")
            except Exception as exc:
                _record(checks, "earth_engine", "fail", str(exc))
    else:
        _record(checks, "earth_engine", "skip", "online initialization disabled")

    failures = [item for item in checks if item["status"] == "fail"]
    return {
        "ok": not failures,
        "mode": mode,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_executable": sys.executable,
        },
        "project": project,
        "output": str(output),
        "checks": checks,
        "failure_count": len(failures),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.minimum_free_gib < 0:
        raise SystemExit("--minimum-free-gib must be non-negative")
    report = run_checks(
        mode=args.mode,
        project=args.project,
        output=args.output,
        minimum_free_gib=args.minimum_free_gib,
        skip_ee=args.skip_ee,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"AEF-GRiTS environment: {report['platform']['system']} "
            f"{report['platform']['machine']} | mode={report['mode']}"
        )
        for check in report["checks"]:
            print(f"[{check['status'].upper():4}] {check['name']}: {check['detail']}")
        print("READY" if report["ok"] else f"NOT READY ({report['failure_count']} failed checks)")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
