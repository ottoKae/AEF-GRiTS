"""Smoke-test an installed wheel from outside the source checkout (no EE requests)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)

    import webapp
    from aef_grits.resources import BUILTIN_MGRS_INDEX

    package = Path(webapp.__file__).resolve().parent
    checkout = Path(__file__).resolve().parents[1]
    assert not package.is_relative_to(checkout), f"Imported source instead of wheel: {package}"
    assert BUILTIN_MGRS_INDEX.is_file()
    assets = ("index.html", "tokens.css", "base.css", "portal.css", "portal.js",
              "leaflet/leaflet.js", "leaflet/leaflet.css")
    for asset in assets:
        assert (package / "static" / asset).is_file(), asset
    for forbidden in ("output", "runs", "acceptance", "server.pid", "server.stdout.log", "server.stderr.log"):
        assert not (package / forbidden).exists(), forbidden

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("AEF_GRITS_WEB_STAGING", None)
    env["AEF_GRITS_WEB_AUTH_MODE"] = "local"
    env["AEF_GRITS_WEB_STATE"] = str(work / "state")
    env["AEF_GRITS_WEB_OUTPUT"] = str(work / "output")
    env["AEF_GRITS_RESOURCE_STATE"] = str(work / "resources")
    subprocess.run(
        [sys.executable, "-m", "webapp.launcher", "--help"],
        cwd=work, env=env, check=True, timeout=30,
    )
    assert not (work / "state").exists(), "--help created task state"
    for module in ("scripts.stream_aef_grid_ee", "scripts.stream_aef_points_ee", "aef_grits.auth_cli"):
        subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=work, env=env, check=True, timeout=30, stdout=subprocess.DEVNULL,
        )

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (work / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "webapp.launcher", "--no-browser", "--port", str(port)],
            cwd=work, env=env, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError((work / "server.log").read_text(encoding="utf-8"))
                try:
                    with urlopen(f"http://127.0.0.1:{port}/portal", timeout=1) as response:
                        assert "AEF-GRiTS" in response.read().decode("utf-8")
                    break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise AssertionError("Installed local server did not start")
            for asset in assets:
                with urlopen(f"http://127.0.0.1:{port}/static/{asset}", timeout=5) as response:
                    assert response.status == 200
                    assert response.read()
            # OPTIONS checks route registration without opening a native dialog.
            request = Request(f"http://127.0.0.1:{port}/api/output-directories/pick", method="OPTIONS")
            with urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert "POST" in response.headers["Allow"]
        finally:
            process.terminate()
            process.wait(timeout=15)
    print(f"Installed wheel verified outside checkout: {package}")


if __name__ == "__main__":
    main()
