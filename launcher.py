"""Start DL Terrain Slicer and open it in the browser.

This is the entry point for both the packaged downloads (PyInstaller freezes
this file) and start.bat, so there is only one startup path to keep working.

    python launcher.py                 # pick a free port, open the browser
    python launcher.py --port 9000     # insist on one port
    python launcher.py --no-browser
    python launcher.py --selftest      # start, check /api/version, exit

The port is negotiated rather than hard-coded: a second copy of the app must
never fight the one already running on 8765.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# frozen builds have no notion of "the folder above this file"
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_PORT = 8765
PORT_ATTEMPTS = 20


def port_is_free(port: int) -> bool:
    """True if nothing is listening on the port and we could bind it.

    Deliberately NO SO_REUSEADDR: on Windows that option lets a bind succeed
    on a port that already has a listener, which would report a busy port as
    free and hand the user a server that dies on startup. The connect probe
    catches listeners the bind test might still let through.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port(preferred: int, attempts: int = PORT_ATTEMPTS) -> int:
    """First free port at or above `preferred`."""
    for port in range(preferred, preferred + attempts):
        if port_is_free(port):
            return port
    raise SystemExit(
        f"No free port between {preferred} and {preferred + attempts - 1}. "
        f"Close the other copy of the app and try again.")


def wait_until_up(url: str, timeout: float = 30.0) -> dict | None:
    """Poll /api/version until the server answers. Returns its JSON, or None."""
    import json
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.2)
    return None


def serve(port: int) -> None:
    """Run uvicorn in this thread until the process is stopped.

    The app object is passed directly. Uvicorn's "app.main:app" import-string
    form re-imports the module in a way that does not survive freezing.
    """
    import uvicorn
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def selftest(port: int) -> int:
    """Prove a build actually runs: start the server, ask it its version.

    This is what CI runs on the packaged artifact - a build can compile
    perfectly and still fail at startup on a missing bundled library.
    """
    from app.core import APP_BUILD
    thread = threading.Thread(target=serve, args=(port,), daemon=True)
    thread.start()
    info = wait_until_up(f"http://127.0.0.1:{port}/api/version")
    if info is None:
        print("SELFTEST FAILED: the server did not answer within 30 s")
        return 1
    if info.get("build") != APP_BUILD:
        print(f"SELFTEST FAILED: served build {info.get('build')}, "
              f"expected {APP_BUILD}")
        return 1
    # the frontend is a separate bundling problem from the Python side
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            shell = r.read()
    except Exception as exc:  # noqa: BLE001 - the message is the point
        print(f"SELFTEST FAILED: could not serve the page: {exc}")
        return 1
    # A STRUCTURAL marker, not display text: checking for the app title here
    # broke the CI selftest on every platform the day the title was reworded
    # (build 21, "DL Terrain Slicer" -> "Terrain Slicer"). The dropzone is the
    # app's core element and its id is not subject to wording passes.
    if b'id="dropzone"' not in shell:
        print("SELFTEST FAILED: index.html was served but looks wrong")
        return 1
    print(f"SELFTEST OK: build {APP_BUILD} serving on port {port}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Start DL Terrain Slicer.")
    ap.add_argument("--port", type=int, default=None,
                    help=f"use this exact port instead of the first free one "
                         f"from {DEFAULT_PORT}")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser window")
    ap.add_argument("--selftest", action="store_true",
                    help="start, verify the app answers, exit (for CI)")
    args = ap.parse_args()

    port = args.port if args.port is not None else free_port(DEFAULT_PORT)

    if args.selftest:
        return selftest(port)

    url = f"http://localhost:{port}"
    print("DL Terrain Slicer")
    print(f"  {url}")
    if port != DEFAULT_PORT:
        print(f"  (port {DEFAULT_PORT} was busy - another copy may be running)")
    print("  close this window to stop the app")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        serve(port)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
