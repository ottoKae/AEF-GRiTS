"""Start the installed local Web GUI without requiring a repository checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import webbrowser

from .paths import default_output_root, default_state_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5555, help="Loopback HTTP port (default: 5555)")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    parser.add_argument("--output-root", type=Path, help="Local directory for downloaded data")
    parser.add_argument("--state-dir", type=Path, help="Local directory for task state and logs")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    for name, explicit, default in (
        ("AEF_GRITS_WEB_OUTPUT", args.output_root, default_output_root()),
        ("AEF_GRITS_WEB_STATE", args.state_dir, default_state_dir()),
    ):
        selected = explicit if explicit is not None else os.environ.get(name, str(default))
        os.environ[name] = str(Path(selected).expanduser().absolute())

    # Import only after parsing: --help must not initialize storage, tasks or credentials.
    try:
        import psutil
        from werkzeug.serving import make_server

        # Importing app recovers interrupted tasks; never run recovery over a live instance.
        pid_path = Path(os.environ["AEF_GRITS_WEB_STATE"]) / "server.pid"
        if pid_path.exists():
            try:
                previous_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError) as exc:
                print(f"Cannot read server PID file {pid_path}: {exc}", file=sys.stderr)
                return 1
            if psutil.pid_exists(previous_pid):
                print(
                    f"This state directory is already in use by PID {previous_pid}. "
                    "Use the running GUI or stop it before restarting; for an independent "
                    "instance choose another --state-dir.",
                    file=sys.stderr,
                )
                return 1
        from webapp import app as application
    except ModuleNotFoundError as exc:
        print(
            f"Missing dependency: {exc.name}. Install this package with [download,web] "
            f"using the same Python interpreter: {sys.executable}",
            file=sys.stderr,
        )
        return 1

    try:
        server = make_server("127.0.0.1", args.port, application.app, threaded=True)
    except (OSError, SystemExit):
        print(f"Cannot listen on 127.0.0.1:{args.port}; choose another --port.", file=sys.stderr)
        return 1

    application.SERVER_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    url = f"http://127.0.0.1:{args.port}/portal"
    print(f"AEF-GRiTS: {url}", flush=True)
    print(f"Data directory: {application.OUTPUT_ROOT}", flush=True)
    print("Keep this process running while downloading. Press Ctrl+C to stop.", flush=True)
    try:
        if not args.no_browser:
            try:
                if not webbrowser.open(url):
                    print("Open the URL above in your browser.", flush=True)
            except (OSError, webbrowser.Error) as exc:
                print(f"Could not open a browser ({exc}); use the URL above.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        application._shutdown_processes()
        application._remove_server_pid()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
