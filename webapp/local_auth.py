"""User-triggered, process-isolated local authentication for the Web GUI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Any

from aef_grits.auth import project_fingerprint
from aef_grits.redaction import redact_text
from aef_grits.storage_safety import terminate_process_group_once


_TIMEOUTS = {"status": 20, "login": 600, "verify": 90}


class LocalAuthRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._state: dict[str, Any] = {"state": "idle", "verified": False}

    def start(
        self, action: str, *, project: str = "", source: str = "auto", force: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(action, str) or action not in _TIMEOUTS:
            raise ValueError("Choose status, login or verify")
        if not isinstance(source, str) or source not in {"auto", "earthengine", "adc"}:
            raise ValueError("Unsupported local credential source")
        if not isinstance(project, str) or not isinstance(force, bool):
            raise ValueError("Project must be text and force must be a boolean")
        project = project.strip()
        if action == "verify" and not project:
            raise ValueError("Enter your Earth Engine Project ID")
        if action == "login" and source == "adc":
            raise ValueError("Use the existing ADC CLI login, or choose Earth Engine login")
        if action == "login":
            source = "earthengine"
        command = [sys.executable, "-m", "aef_grits.auth_cli", action]
        if action != "status":
            command.extend(["--source", source])
        if action == "login":
            command.extend(["--auth-mode", "localhost"])
            if force:
                command.append("--force")
        else:
            command.append("--json")
        if action == "verify":
            command.append("--check-aef")
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        if project:
            # Project configuration stays out of the process command line and UI logs.
            environment["AEF_GRITS_PROJECT"] = project
        with self._lock:
            if self._state["state"] == "running" or (self._process is not None and self._process.poll() is None):
                raise RuntimeError("An authentication operation is already running")
            self._state = {
                "state": "running", "action": action, "source": source,
                "project_fingerprint": project_fingerprint(project) if project else None,
                "verified": False,
            }
            try:
                self._process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                    env=environment, start_new_session=os.name == "posix",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError:
                self._state.update(state="error", error="Could not start the local authentication command")
                return dict(self._state)
            threading.Thread(
                target=self._finish, args=(self._process, action, project), daemon=True,
            ).start()
            return dict(self._state)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def is_verified(self, project: str, source: str = "auto") -> bool:
        with self._lock:
            return bool(
                self._state.get("verified")
                and self._state.get("source") == source
                and self._state.get("project_fingerprint") == project_fingerprint(project.strip())
            )

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if self._state["state"] != "running" or process is None:
                return dict(self._state)
            self._state.update(state="cancelled", verified=False)
        terminate_process_group_once(process)
        return self.status()

    def _finish(self, process: subprocess.Popen[str], action: str, project: str) -> None:
        result: dict[str, Any]
        try:
            stdout, _ = process.communicate(timeout=_TIMEOUTS[action])
            # Authentication libraries can print notices before the final CLI JSON.
            decoder = json.JSONDecoder()
            payload = None
            for position, character in enumerate(stdout):
                if character == "{":
                    try:
                        candidate, consumed = decoder.raw_decode(stdout[position:])
                    except ValueError:
                        continue
                    if not stdout[position + consumed:].strip() and isinstance(candidate, dict):
                        payload = candidate
                        break
            if payload is None:
                result = {"state": "error", "error": "Authentication command failed; no valid result was returned"}
            elif process.returncode or payload.get("ok") is False:
                message = redact_text(payload.get("error", "Authentication failed")).lower()
                categories = (
                    (("credentials or project",), "Earth Engine initialization failed; check credentials, project permissions and network connectivity"),
                    (("credential", "authenticate", "invalid_grant"), "Credentials are missing or invalid; explicitly sign in again"),
                    (("permission", "forbidden", "not authorized", "403"), "The current identity cannot access this project or AEF collection"),
                    (("project", "api has not been", "not registered"), "Check the Project ID, Earth Engine registration and API configuration"),
                    (("quota", "429", "resource exhausted"), "The project quota is exhausted; retry after the limit resets"),
                    (("timeout", "timed out", "connection", "ssl", "network"), "Network verification failed; check connectivity and retry"),
                )
                error = next(
                    (description for tokens, description in categories if any(token in message for token in tokens)),
                    "Authentication or AEF verification failed; inspect the configuration and retry",
                )
                # Provider errors can contain credentials and IDs: return categories, never raw output.
                result = {"state": "error", "error": error}
            else:
                result = {"state": "done"}
                if action == "status":
                    result["candidates"] = [
                        {"source": item["source"], "available": bool(item["available"]),
                         "configured_by": item["configured_by"]}
                        for item in payload.get("candidates", [])
                    ]
                elif action == "verify":
                    result["verified"] = payload.get("aef_available") is True
                    result["aef_available"] = result["verified"]
                    result["authentication"] = {
                        key: value for key, value in payload.get("authentication", {}).items()
                        if key in {"source", "credential_type", "credential_version",
                                   "principal_fingerprint", "project_fingerprint"}
                    }
        except subprocess.TimeoutExpired:
            terminate_process_group_once(process)
            result = {"state": "error", "error": "Authentication timed out; retry explicitly after checking the connection"}
        except (OSError, ValueError, KeyError, TypeError):
            result = {"state": "error", "error": "Could not read the authentication result"}
        with self._lock:
            if self._process is process and self._state["state"] == "running":
                self._state.update(result)
