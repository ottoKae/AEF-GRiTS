from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from webapp import folder_picker


def test_help_is_noninteractive_and_creates_no_files(tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run(
        [sys.executable, "-m", "webapp.folder_picker", "--help"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0
    assert "--initial-dir" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("selected", [None, "中文 output folder"])
def test_native_dialog_result_and_cancel(monkeypatch, tmp_path, capsys, selected):
    tk = pytest.importorskip("tkinter")
    from tkinter import filedialog

    calls = []

    class Window:
        def withdraw(self):
            calls.append("withdraw")

        def attributes(self, *args):
            calls.append(args)

        def destroy(self):
            calls.append("destroy")

    window = Window()
    monkeypatch.setattr(tk, "Tk", lambda: window)

    def choose(**kwargs):
        assert kwargs["parent"] is window
        assert kwargs["initialdir"] == str(tmp_path)
        assert kwargs["mustexist"] is True
        return str(tmp_path / selected) if selected else ""

    monkeypatch.setattr(filedialog, "askdirectory", choose)
    assert folder_picker.main(["--initial-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == (str((tmp_path / selected).resolve()) if selected else None)
    assert calls == ["withdraw", ("-topmost", True), "destroy"]


def test_native_dialog_headless_error(monkeypatch, tmp_path, capsys):
    tk = pytest.importorskip("tkinter")

    def unavailable():
        raise tk.TclError("no display")

    monkeypatch.setattr(tk, "Tk", unavailable)
    assert folder_picker.main(["--initial-dir", str(tmp_path)]) == 1
    assert "local desktop session" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("selection", [None, "中文 data"])
def test_caller_runs_bounded_child_and_decodes_selection(monkeypatch, tmp_path, selection):
    original_run = subprocess.run
    result_path = str(tmp_path / selection) if selection else None

    def child(command, **kwargs):
        assert command == [sys.executable, "-m", "webapp.folder_picker", "--initial-dir", str(tmp_path)]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 5
        # Only the native dialog is substituted; the result crosses a real process pipe.
        return original_run([sys.executable, "-c", f"print({json.dumps({'path': result_path})!r})"], **kwargs)

    monkeypatch.setattr(folder_picker.subprocess, "run", child)
    assert folder_picker.pick_directory(tmp_path, timeout_seconds=5) == result_path


@pytest.mark.parametrize("stdout,returncode,expected", [
    ('{"error":"Tkinter is unavailable"}', 1, "Tkinter"),
    ('not json', 0, "Could not read"),
    ('{"path":42}', 0, "Could not read"),
])
def test_caller_reports_native_and_malformed_errors(monkeypatch, tmp_path, stdout, returncode, expected):
    monkeypatch.setattr(folder_picker.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], returncode, stdout, ""))
    with pytest.raises(RuntimeError, match=expected):
        folder_picker.pick_directory(tmp_path)


def test_caller_reports_timeout(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(folder_picker.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        folder_picker.pick_directory(tmp_path)
