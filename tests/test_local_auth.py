from __future__ import annotations

import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from aef_grits import auth_cli
from webapp import local_auth


@pytest.fixture
def run_child(monkeypatch):
    original = subprocess.Popen
    calls = []

    def configure(payload=None, *, delay=0, returncode=0, banner=""):
        code = (
            f"import json,time; time.sleep({delay}); print({banner!r}); "
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
