from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, session

from aef_grits.auth_vault import CredentialVault, create_vault_key
from webapp.auth import WebAuth
from webapp.auth import AuthBinding
import webapp.app as web


def _oauth_auth(monkeypatch, tmp_path) -> WebAuth:
    client = tmp_path / "oauth-client.json"
    client.write_text(json.dumps({"web": {}}), encoding="utf-8")
    key = create_vault_key(tmp_path / "vault.key")
    monkeypatch.setenv("AEF_GRITS_WEB_AUTH_MODE", "oauth")
    monkeypatch.setenv("AEF_GRITS_WEB_OAUTH_CLIENT", str(client))
    monkeypatch.setenv("AEF_GRITS_WEB_OAUTH_REDIRECT_URI", "https://aef.example/auth/callback")
    monkeypatch.setenv("AEF_GRITS_AUTH_VAULT", str(tmp_path / "vault.sqlite"))
    monkeypatch.setenv("AEF_GRITS_AUTH_VAULT_KEY", str(key))
    return WebAuth()


def test_oauth_binding_uses_current_users_encrypted_credentials(monkeypatch, tmp_path):
    manager = _oauth_auth(monkeypatch, tmp_path)
    assert manager.vault is not None
    manager.vault.put(
        "subject-a",
        {
            "token": "short-lived",
            "refresh_token": "refresh-secret",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scopes": ["https://www.googleapis.com/auth/earthengine"],
            "expiry": None,
        },
    )
    calls = []
    import ee

    monkeypatch.setattr(ee, "Initialize", lambda **kwargs: calls.append(kwargs))
    flask = Flask(__name__)
    flask.secret_key = "test-secret"
    with flask.test_request_context("/"):
        session["aef_google_sub"] = "subject-a"
        session["aef_email"] = "a@example.edu"
        binding = manager.bind_project(session, "gee-test")
        assert binding.auth_source == "web"
        assert binding.credential_handle.startswith("cred_")
        assert binding.owner_hash.startswith("user_")
        assert calls[0]["project"] == "gee-test"
        assert calls[0]["credentials"].refresh_token == "refresh-secret"


class _FakeSharedAuth:
    mode = "oauth"
    allow_insecure = False

    def __init__(self, authenticated: bool, owner: str = "user_b"):
        self.authenticated = authenticated
        self.owner = owner

    def status(self, session):
        return {"mode": "oauth", "authenticated": self.authenticated}

    def current_owner_hash(self, session):
        if not self.authenticated:
            raise PermissionError
        return self.owner


def test_shared_api_requires_login(monkeypatch):
    monkeypatch.setattr(web, "_web_auth", _FakeSharedAuth(False))
    client = web.app.test_client()
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/capabilities").status_code == 401


def test_shared_tasks_are_private_to_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_web_auth", _FakeSharedAuth(True, "user_b"))
    rid = "private-task"
    web._tasks[rid] = {
        "owner_hash": "user_a",
        "status": "done",
        "progress": 1.0,
        "params": {"workflow": "points", "years": [2025]},
        "output_dir": tmp_path,
        "hidden": False,
    }
    try:
        client = web.app.test_client()
        assert client.get("/api/tasks").get_json() == []
        assert client.get(f"/api/tasks/{rid}").status_code == 404
        assert client.delete(f"/api/tasks/{rid}").status_code == 404
    finally:
        web._tasks.pop(rid, None)


def test_web_worker_command_contains_no_project_or_credential_handle(tmp_path):
    params = {
        "workflow": "points",
        "samples_path": str(tmp_path / "points.csv"),
        "auth_source": "web",
        "project": "gee-test-private",
        "years": [2025],
        "workers": 1,
        "resolved_workers": 1,
        "max_retries": 1,
        "request_timeout_seconds": 30,
        "chunk_size": 100,
        "page_size": 100,
        "geometry_mode": "auto",
        "max_points": 1000,
        "id_field": "",
        "layer": "",
        "reference_grid": "",
    }
    command = web._build_cmd(params, tmp_path / "output")
    rendered = " ".join(command)
    assert "gee-test-private" not in rendered
    assert "credential_handle" not in rendered
    assert "--project" not in command


def test_download_modules_never_call_interactive_authentication():
    root = Path(__file__).resolve().parents[1]
    allowed = root / "aef_grits" / "auth_cli.py"
    findings = []
    for path in [*(root / "aef_grits").glob("*.py"), *(root / "scripts").glob("*.py")]:
        if path == allowed:
            continue
        if ".Authenticate(" in path.read_text(encoding="utf-8", errors="replace"):
            findings.append(path.name)
    assert findings == []


class _VersionedAuth:
    mode = "local"
    allow_insecure = False

    def __init__(self):
        self.version = "v1"

    def current_owner_hash(self, session):
        return "user_plan"

    def current_binding(self, session, project, verify=False):
        return AuthBinding(
            owner_hash="user_plan",
            auth_source="auto",
            credential_handle=None,
            credential_version=self.version,
            project_fingerprint="project_fp",
        )


def test_signed_plan_is_invalidated_when_credential_version_changes(monkeypatch, tmp_path):
    manager = _VersionedAuth()
    monkeypatch.setattr(web, "_web_auth", manager)
    monkeypatch.setattr(web, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(web, "OUTPUT_ROOT", tmp_path / "output")
    web.PLANS_DIR.mkdir()
    web.OUTPUT_ROOT.mkdir()
    with web.app.test_request_context("/"):
        response = web._write_plan(
            {"project": "gee-test", "workflow": "grid"},
            {"workflow": "grid", "raw_bytes": 0, "grids": [], "years": [2025]},
            auth_binding=manager.current_binding(session, "gee-test"),
        )
        manager.version = "v2"
        try:
            web._read_plan(response["plan_id"])
        except ValueError as exc:
            assert "credential version" in str(exc)
        else:
            raise AssertionError("credential change did not invalidate the signed plan")


def test_auth_required_event_marks_task_for_checkpointed_pause(tmp_path, monkeypatch):
    rid = "auth-event"
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path / "runs")
    log_path = web.RUNS_DIR / rid / "run.log"
    log_path.parent.mkdir(parents=True)
    web._tasks[rid] = {
        "status": "running",
        "progress": 0.0,
        "params": {"workflow": "points"},
        "grid_progress": {},
        "log_path": log_path,
        "output_dir": tmp_path / "output",
    }
    try:
        web._update_progress(
            rid,
            'AEF_EVENT {"event":"auth_required","error_type":"CredentialVerificationError"}',
        )
        assert web._tasks[rid]["auth_failure"] is True
    finally:
        web._tasks.pop(rid, None)
