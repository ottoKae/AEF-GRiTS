"""Explicit authentication commands for AEF-GRiTS.

This is the only module allowed to start an interactive login flow. Download
commands never import or call these login functions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .auth import (
    AUTH_SOURCES,
    EARTH_ENGINE_SCOPES,
    AuthenticationError,
    discover_credentials,
    earth_engine_credentials_path,
)
from .earth_engine import initialize


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Inspect existing credentials without login")
    status.add_argument("--json", action="store_true")

    login = subparsers.add_parser("login", help="Explicitly start a user-selected login flow")
    login.add_argument("--source", choices=("earthengine", "adc"), default="earthengine")
    login.add_argument(
        "--auth-mode",
        choices=("localhost", "gcloud", "notebook"),
        default="localhost",
    )
    login.add_argument("--project")
    login.add_argument("--force", action="store_true")

    verify = subparsers.add_parser("verify", help="Verify existing credentials and project online")
    verify.add_argument("--source", choices=AUTH_SOURCES, default="auto")
    verify.add_argument("--project")
    verify.add_argument("--json", action="store_true")

    logout = subparsers.add_parser("logout", help="Explicitly revoke/remove one local source")
    logout.add_argument("--source", choices=("earthengine", "adc"), required=True)
    logout.add_argument("--yes", action="store_true", help="Confirm credential removal/revocation")

    vault = subparsers.add_parser("vault-init", help="Initialize an encrypted Web credential vault")
    vault.add_argument("--database", type=Path, required=True)
    vault.add_argument("--key-file", type=Path, required=True)
    return parser.parse_args(argv)


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _gcloud() -> str:
    executable = shutil.which("gcloud")
    if not executable:
        raise AuthenticationError("gcloud is required for ADC login but was not found")
    return executable


def _login(args: argparse.Namespace) -> dict[str, Any]:
    if args.source == "earthengine":
        import ee

        ee.Authenticate(auth_mode=args.auth_mode, force=args.force)
        if args.project:
            executable = shutil.which("earthengine")
            if not executable:
                raise AuthenticationError("earthengine CLI is unavailable for set_project")
            _run([executable, "set_project", args.project])
    else:
        if args.force:
            subprocess.run(
                [_gcloud(), "auth", "application-default", "revoke", "--quiet"],
                check=False,
            )
        command = [
            _gcloud(),
            "auth",
            "application-default",
            "login",
            "--scopes=" + ",".join(EARTH_ENGINE_SCOPES),
        ]
        _run(command)
        if args.project:
            _run(
                [
                    _gcloud(),
                    "auth",
                    "application-default",
                    "set-quota-project",
                    args.project,
                ]
            )
    return {"ok": True, "source": args.source, "project_configured": bool(args.project)}


def _logout(args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise AuthenticationError("Credential removal requires --yes")
    if args.source == "earthengine":
        path = earth_engine_credentials_path()
        if path.exists():
            path.unlink()
    else:
        _run([_gcloud(), "auth", "application-default", "revoke", "--quiet"])
    return {"ok": True, "source": args.source, "removed": True}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            payload = {
                "interactive_login_started": False,
                "candidates": [item.as_dict() for item in discover_credentials()],
            }
        elif args.command == "login":
            payload = _login(args)
        elif args.command == "verify":
            _, resolved = initialize(
                args.project, auth_source=args.source, return_auth=True
            )
            payload = {"ok": True, "authentication": resolved.public_summary()}
        elif args.command == "logout":
            payload = _logout(args)
        else:
            from .auth_vault import CredentialVault, create_vault_key

            create_vault_key(args.key_file)
            CredentialVault(args.database, args.key_file)
            payload = {
                "ok": True,
                "database": str(args.database.expanduser().absolute()),
                "key_file": str(args.key_file.expanduser().absolute()),
            }
    except (AuthenticationError, FileExistsError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
