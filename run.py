"""Verba entry point.

Desktop mode (default): binds 127.0.0.1 and opens the browser once the server is up.
Server mode (--server): binds 0.0.0.0 (or --host) and never opens a browser.

Missing *core* dependencies are installed automatically into the current
environment so that `start.bat` / `start.sh` stay trivial. Heavy feature groups
(whisper, embeddings, ...) are installed later by the in-app onboarding.

Frozen mode (PyInstaller build): core dependencies are bundled, feature groups
are pip-installed into <data>/site-packages at runtime — that directory is put
on sys.path here, before anything imports from it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
PROJECT_ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "backend"
CORE_REQUIREMENTS = PROJECT_ROOT / "requirements" / "core.txt"

MIN_PYTHON = (3, 11)


def ensure_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, found: {sys.version.split()[0]}"
        )


def ensure_streams() -> None:
    """Windowed PyInstaller builds run without stdout/stderr — give the stdlib
    something writable so prints and logging handlers never crash. File logs
    (data/logs/) stay the real diagnostic channel."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — lives forever
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — lives forever


def ensure_core_dependencies() -> None:
    if FROZEN:
        return  # bundled by PyInstaller
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("Installing core dependencies (one-time) ...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(CORE_REQUIREMENTS)]
        )
        print("Core dependencies installed.")


def loopback_sockets(port: int) -> list:
    """Desktop mode binds the full loopback — 127.0.0.1 AND ::1 — so that
    "localhost" works no matter whether the OS resolves it to IPv4 or IPv6
    (Windows often prefers ::1). Returns [] when nothing could be bound."""
    import socket

    sockets = []
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.bind((address, port))
            sockets.append(sock)
        except OSError:
            continue  # address family unavailable — bind what we can
    return sockets


def open_browser_when_ready(url: str, health_url: str, timeout: float = 30.0) -> None:
    import urllib.request
    import webbrowser

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.3)


def main() -> None:
    ensure_streams()
    ensure_python_version()
    ensure_core_dependencies()

    parser = argparse.ArgumentParser(prog="verba", description="Start Verba")
    parser.add_argument("--server", action="store_true", help="server mode: 0.0.0.0, no browser")
    parser.add_argument("--host", default=None, help="bind address (overrides the mode default)")
    parser.add_argument("--port", type=int, default=None, help="port (default from settings, 8710)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--data-dir", default=None, help="data directory (default: ./data)")
    args = parser.parse_args()

    if args.data_dir:
        os.environ["VERBA_DATA_DIR"] = str(Path(args.data_dir).resolve())

    if not FROZEN:
        sys.path.insert(0, str(BACKEND_DIR))
    from verba.config import ensure_runtime_site_packages, get_settings

    ensure_runtime_site_packages()

    settings = get_settings()
    host = args.host or ("0.0.0.0" if args.server else "127.0.0.1")
    port = args.port or settings.server.port

    if not args.server and not args.no_browser:
        browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        url = f"http://{browse_host}:{port}/"
        threading.Thread(
            target=open_browser_when_ready, args=(url, url + "health"), daemon=True
        ).start()

    import uvicorn

    from verba.main import create_app

    config = uvicorn.Config(
        create_app,
        factory=True,
        host=host,
        port=port,
        log_config=None,  # logging is configured by the app itself (with rotation)
    )
    server = uvicorn.Server(config)
    # default desktop binding: serve IPv4 and IPv6 loopback simultaneously
    sockets = loopback_sockets(port) if host == "127.0.0.1" else []
    server.run(sockets=sockets or None)


if __name__ == "__main__":
    main()
