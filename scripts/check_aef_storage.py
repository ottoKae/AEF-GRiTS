#!/usr/bin/env python
"""Validate final/state/staging filesystems without blocking the caller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aef_grits.storage_safety import isolated_disk_free, validate_storage_layout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    layout = validate_storage_layout(args.final, args.state, args.staging)
    incident = args.state / "filesystem_incident.json"
    result = {
        "status": "healthy",
        "layout": layout.as_dict(),
        "free_bytes": {
            "final": isolated_disk_free(
                args.final, timeout_seconds=args.timeout, incident_path=incident
            ),
            "state": isolated_disk_free(
                args.state, timeout_seconds=args.timeout, incident_path=incident
            ),
            "staging": isolated_disk_free(
                args.staging, timeout_seconds=args.timeout, incident_path=incident
            ),
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
