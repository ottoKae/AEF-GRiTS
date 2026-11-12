from __future__ import annotations

import json
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from aef_grits import auth_cli
from webapp import local_auth


AUTH_URL = "https://accounts.google.com/o/oauth2/auth?" + urlencode({
    "client_id": "test.apps.googleusercontent.com", "scope": "test-scope",
    "redirect_uri": "http://localhost:8085", "response_type": "code",
    "code_challenge": "test-challenge", "code_challenge_method": "S256",
})


@pytest.fixture
def run_child(monkeypatch):
    original = subprocess.Popen
    calls = []

    def configure(payload=None, *, delay=0, returncode=0, banner="", stream=""):
        code = (
            f"import json,time; print({stream!r}); time.sleep({delay}); print({banner!r}); "
            f"print(json.dumps({payload!r})); raise SystemExit({returncode})"
        )

        def launch(command, **kwargs):
            calls.append((command, kwargs.get("env", {})))
            return original([sys.executable, "-c", code], **kwargs)

        monkeypatch.setattr(local_auth.subprocess, "Popen", launch)
        return calls

    return configure


def completed(runner):
    deadline = time.monotonic() + 6
    while runner.status()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    state = runner.status()
    assert state["state"] != "running"
    return state


def test_discovery_is_not_authentication(run_child):
    calls = run_child({"interactive_login_started": False, "candidates": [
        {"source": "earthengine", "available": True, "configured_by": "Earth Engine persistent credentials"},
    ]})
    runner = local_auth.LocalAuthRunner()
    runner.start("status")
    state = completed(runner)
    assert state["state"] == "done"
    assert state["candidates"][0]["available"] is True
    assert not state["verified"]
    assert calls[0][0] == [sys.executable, "-m", "aef_grits.auth_cli", "status", "--json"]


def test_verification_is_bound_to_project_and_requested_source(run_child):
    calls = run_child({"ok": True, "aef_available": True, "authentication": {
        "source": "earthengine", "credential_version": "v1", "access_token": "not-for-the-ui",
    }}, banner="A notice before JSON")
    runner = local_auth.LocalAuthRunner()
    runner.start("verify", project="private-project", source="auto")
    state = completed(runner)
    assert runner.is_verified("private-project", "auto")
    assert not runner.is_verified("other-project", "auto")
    assert not runner.is_verified("private-project", "adc")
    assert "private-project" not in json.dumps(state)
    assert "not-for-the-ui" not in json.dumps(state)
    assert "--check-aef" in calls[0][0]
    assert "private-project" not in calls[0][0]
    assert calls[0][1]["AEF_GRITS_PROJECT"] == "private-project"


def test_login_does_not_claim_project_verified(run_child):
    calls = run_child({"ok": True, "source": "earthengine"})
    runner = local_auth.LocalAuthRunner()
    runner.start("login", force=True)
    state = completed(runner)
    assert state["state"] == "done" and not state["verified"]
    assert calls[0][0][-3:] == ["--auth-mode", "localhost", "--force"]


def test_login_streams_google_url_before_callback_and_clears_on_success(run_child):
    calls = run_child({"ok": True}, delay=0.5, stream=AUTH_URL)
    runner = local_auth.LocalAuthRunner()
    runner.start("login")
    deadline = time.monotonic() + 3
    while not runner.status().get("authorization_url") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.status()["state"] == "running"
    assert runner.status()["authorization_url"] == AUTH_URL
    assert calls[0][1]["PYTHONUNBUFFERED"] == "1"
    assert "authorization_url" not in completed(runner)


@pytest.mark.parametrize("url", [
    "https://untrusted.example/login",
    AUTH_URL.replace("accounts.google.com", "accounts.google.com.untrusted.example"),
    AUTH_URL.replace("response_type=code", "response_type=token"),
    AUTH_URL + "&access_token=not-for-the-ui",
    AUTH_URL.replace("localhost", "untrusted.example"),
])
def test_login_never_exposes_non_authorization_output(run_child, url):
    run_child({"ok": True}, delay=0.2, stream=url)
    runner = local_auth.LocalAuthRunner()
    runner.start("login")
    deadline = time.monotonic() + 3
    while runner.status()["state"] == "running" and time.monotonic() < deadline:
        assert "authorization_url" not in runner.status()
        time.sleep(0.01)
    assert completed(runner)["state"] == "done"
    assert "not-for-the-ui" not in json.dumps(runner.status())


def test_cancel_removes_pending_authorization_url(run_child):
    run_child({"ok": True}, delay=30, stream=AUTH_URL)
    runner = local_auth.LocalAuthRunner()
    runner.start("login")
    try:
        deadline = time.monotonic() + 3
        while not runner.status().get("authorization_url") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runner.status().get("authorization_url") == AUTH_URL
        state = runner.cancel()
        assert state["state"] == "cancelled"
        assert "authorization_url" not in state
        runner._process.wait(timeout=3)
    finally:
        if runner._process.poll() is None:
            runner._process.kill()


def test_verify_requires_aef_access(run_child):
    run_child({"ok": True, "authentication": {"source": "earthengine"}})
    runner = local_auth.LocalAuthRunner()
    runner.start("verify", project="private-project")
    assert not completed(runner)["verified"]


def test_duplicate_login_and_cancel_real_process(run_child):
    run_child({"ok": True}, delay=30)
    runner = local_auth.LocalAuthRunner()
    runner.start("login")
    process = runner._process
    try:
        with pytest.raises(RuntimeError, match="already running"):
            runner.start("login")
        assert runner.cancel()["state"] == "cancelled"
        process.wait(timeout=3)
        assert runner.status()["state"] == "cancelled"
    finally:
        if process.poll() is None:
            process.kill()


def test_timeout_reaps_process_without_login_retry(monkeypatch, run_child):
    calls = run_child({"ok": True}, delay=30)
    monkeypatch.setitem(local_auth._TIMEOUTS, "verify", 0.1)
    runner = local_auth.LocalAuthRunner()
    runner.start("verify", project="private-project")
    state = completed(runner)
    assert state["state"] == "error" and "timed out" in state["error"]
    runner._process.wait(timeout=3)
    assert len(calls) == 1


def test_error_does_not_expose_provider_response(run_child):
    run_child({"ok": False, "error": 'permission denied private-project "access_token": "secret"'}, returncode=1)
    runner = local_auth.LocalAuthRunner()
    runner.start("verify", project="private-project")
    state = completed(runner)
    assert state["state"] == "error"
    assert "cannot access" in state["error"]
    assert "secret" not in json.dumps(state) and "private-project" not in json.dumps(state)


@pytest.mark.parametrize("kwargs", [
    {"action": "arbitrary-command"}, {"action": "verify"},
    {"action": "login", "source": "adc"}, {"action": "verify", "source": "web"},
    {"action": "status", "project": []}, {"action": "login", "force": "yes"},
    {"action": []}, {"action": "status", "source": []},
])
def test_input_rejected_before_process_start(kwargs):
    with pytest.raises(ValueError):
        local_auth.LocalAuthRunner().start(**kwargs)


def test_cli_verify_checks_only_one_aef_item(monkeypatch, capsys):
    calls = []
    query = SimpleNamespace(
        limit=lambda count: calls.append(("limit", count)) or query,
        size=lambda: query,
        getInfo=lambda: 1,
    )
    ee = SimpleNamespace(ImageCollection=lambda name: calls.append(("collection", name)) or query)
    resolved = SimpleNamespace(public_summary=lambda: {"source": "earthengine"})

    def initialize(*args, **kwargs):
        assert kwargs["request_timeout_seconds"] == 30
        return ee, resolved

    monkeypatch.setattr(auth_cli, "initialize", initialize)
    assert auth_cli.main(["verify", "--check-aef", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["aef_available"] is True
    assert calls == [("collection", auth_cli.DATASET), ("limit", 1)]
