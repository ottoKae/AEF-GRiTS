from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from webapp import paths


def test_state_defaults_are_user_writable_not_package_relative(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))
    monkeypatch.setattr(paths.sys, "platform", "win32")
    assert paths.default_state_dir() == tmp_path / "local-appdata" / "AEF-GRiTS"
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.default_state_dir() == tmp_path / "Library" / "Application Support" / "AEF-GRiTS"
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert paths.default_state_dir() == tmp_path / "xdg-state" / "aef-grits"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert paths.default_state_dir() == tmp_path / ".local" / "state" / "aef-grits"
    assert paths.default_output_root() == tmp_path / "Downloads" / "AEF-GRiTS"


def _environment(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["AEF_GRITS_WEB_STATE"] = str(tmp_path / "env-state")
    env["AEF_GRITS_WEB_OUTPUT"] = str(tmp_path / "env-output")
    env["AEF_GRITS_RESOURCE_STATE"] = str(tmp_path / "resources")
    env["AEF_GRITS_WEB_AUTH_MODE"] = "local"
    env.pop("AEF_GRITS_WEB_STAGING", None)
    return env


def test_help_does_not_import_app_or_create_state(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "webapp.launcher", "--help"],
        cwd=tmp_path, env=_environment(tmp_path), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--output-root" in result.stdout
    assert not (tmp_path / "env-state").exists()
    assert not (tmp_path / "env-output").exists()
    assert not (tmp_path / "resources").exists()


@pytest.mark.parametrize("port", ["0", "65536"])
def test_invalid_port_has_no_side_effects(port, tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "webapp.launcher", "--port", port],
        cwd=tmp_path, env=_environment(tmp_path), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "between 1 and 65535" in result.stderr
    assert not (tmp_path / "env-state").exists()


def test_launcher_serves_assets_from_outside_repo_and_cli_overrides_env(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    state = tmp_path / "chosen-state"
    output = tmp_path / "chosen-output"
    with (tmp_path / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "webapp.launcher", "--no-browser", "--port", str(port),
             "--state-dir", str(state), "--output-root", str(output)],
            cwd=tmp_path, env=_environment(tmp_path), stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail((tmp_path / "server.log").read_text(encoding="utf-8"))
                try:
                    with urlopen(f"http://127.0.0.1:{port}/portal", timeout=1) as response:
                        assert "AEF-GRiTS" in response.read().decode("utf-8")
                    break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                pytest.fail("Local launcher did not start within 40 seconds")
            for asset in ("portal.js", "portal.css", "leaflet/leaflet.js", "leaflet/leaflet.css"):
                with urlopen(f"http://127.0.0.1:{port}/static/{asset}", timeout=5) as response:
                    assert response.status == 200
                    assert response.read()
            assert (state / "server.pid").read_text() == str(process.pid)
            assert output.is_dir()
            assert not (tmp_path / "env-state").exists()
            assert not (tmp_path / "env-output").exists()
            second = subprocess.run(
                [sys.executable, "-m", "webapp.launcher", "--no-browser", "--port", str(port),
                 "--state-dir", str(state), "--output-root", str(output)],
                cwd=tmp_path, env=_environment(tmp_path), capture_output=True, text=True, timeout=20,
            )
            assert second.returncode == 1
            assert "already in use" in second.stderr
            assert process.poll() is None
            with urlopen(f"http://127.0.0.1:{port}/portal", timeout=5) as response:
                assert response.status == 200
        finally:
            process.terminate()
            process.wait(timeout=15)
