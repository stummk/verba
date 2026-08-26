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
import contextlib
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
    (data/logs/) stay the real diagnostic channel.

    Existing streams are switched to replacement characters: a console
    codepage like cp1252 encodes neither box drawing nor every possible path,
    and a console message must never be what ends the process.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — lives forever
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — lives forever
    for stream in (sys.stdout, sys.stderr):
        # not every stream is a reconfigurable text stream — then there is
        # nothing to fix and nothing to complain about
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")


INTERNAL_PIP_FLAG = "--internal-pip"
INTERNAL_IMPORT_FLAG = "--internal-import"


def run_internal_task(argv: list[str]) -> int | None:
    """Helper modes the setup runs in a child process; None = normal start.

    A frozen build has no interpreter to call, so it re-invokes itself:
    - `--internal-pip <pip args>` installs feature groups. Doing this in a
      child process keeps the server process from ever loading the packages,
      which matters on Windows where a loaded .pyd cannot be replaced.
    - `--internal-import <module>` is the smoke test after an installation.
    """
    if len(argv) < 2 or argv[0] not in (INTERNAL_PIP_FLAG, INTERNAL_IMPORT_FLAG):
        return None
    ensure_streams()
    if not FROZEN:
        sys.path.insert(0, str(BACKEND_DIR))
    from verba.config import ensure_runtime_site_packages

    ensure_runtime_site_packages()

    if argv[0] == INTERNAL_IMPORT_FLAG:
        import importlib

        importlib.import_module(argv[1])
        return 0

    # pip is bundled as a plain file tree, not as frozen modules: its vendored
    # distlib resolves resources only through standard path-based importers
    from verba.config import bundle_root

    pip_lib = bundle_root() / "pip-lib"
    if pip_lib.is_dir() and str(pip_lib) not in sys.path:
        sys.path.insert(0, str(pip_lib))
    from pip._internal.cli.main import main as pip_main

    return int(pip_main(list(argv[1:])))


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


def bind_sockets(host: str, port: int) -> list:
    """Bind the listening socket(s) before the banner is printed.

    Two reasons to do it here instead of leaving it to uvicorn: the banner
    must not promise an address that a port conflict then denies, and an
    occupied port deserves one clear sentence instead of a traceback.
    """
    import socket

    if host == "127.0.0.1":
        sockets = loopback_sockets(port)
        # IPv6-only success means someone else holds the port on IPv4 — most
        # likely a second Verba, which would then share the SQLite database
        # and the job queue with the first one. Refuse instead.
        if not any(sock.family == socket.AF_INET for sock in sockets):
            for sock in sockets:
                sock.close()
            sys.exit(
                f"Port {port} is already in use on 127.0.0.1 - "
                "is Verba already running? Use --port to pick another one."
            )
        return sockets

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name != "nt":  # on Windows this would allow hijacking the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        sys.exit(f"Cannot listen on {host}:{port} - {exc}")
    return [sock]


def local_addresses() -> list[str]:
    """The machine's own non-loopback IP addresses, best effort.

    Needed for the startup banner: with a wildcard bind (0.0.0.0) the port
    alone tells an admin nothing about where the server can be reached, and on
    a headless server there is no browser to try it out with.
    """
    import socket

    found: list[str] = []

    def remember(address: str) -> None:
        if address and address not in found and not address.startswith(("127.", "::1", "fe80")):
            found.append(address)

    try:  # every address the hostname resolves to
        for info in socket.getaddrinfo(socket.gethostname(), None, proto=socket.IPPROTO_TCP):
            remember(info[4][0])
    except OSError:
        pass
    try:
        # the address of the default route: reveals the LAN IP even when the
        # hostname does not resolve. UDP connect() sends nothing, it only
        # picks a route — the target is the reserved documentation address.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            remember(probe.getsockname()[0])
    except OSError:
        pass
    return found


def startup_banner(host: str, port: int, server_mode: bool, data_dir: str) -> str:
    """The address block printed on start — the one thing an admin needs.

    Shown by start.sh / start.bat and, in server mode, captured by systemd,
    so `journalctl -u verba` / `systemctl status verba` answers "which port
    is it on again?" without reading the unit file.
    """
    from verba import __version__

    wildcard = host in ("0.0.0.0", "::")
    # deliberately ASCII: this block has to survive every console codepage
    lines = [f"Verba {__version__} - {'server mode' if server_mode else 'desktop mode'}"]
    if wildcard:
        lines.append(f"  listening on   http://{host}:{port}  (all interfaces)")
        lines.append(f"  local          http://127.0.0.1:{port}")
        for address in local_addresses():
            shown = f"[{address}]" if ":" in address else address
            lines.append(f"  network        http://{shown}:{port}")
    elif host == "127.0.0.1":
        lines.append(f"  listening on   http://localhost:{port}  (127.0.0.1 and [::1])")
    else:
        lines.append(f"  listening on   http://{host}:{port}")
    lines.append(f"  data directory {data_dir}")
    lines.append("  stop with Ctrl+C")
    rule = "-" * max(len(line) for line in lines)
    return "\n".join([rule, *lines, rule])


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

    internal = run_internal_task(sys.argv[1:])
    if internal is not None:
        sys.exit(internal)

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
    from verba.config import ensure_runtime_site_packages, get_settings, repair_site_packages

    repair_site_packages()  # before anything imports (and locks) from there
    ensure_runtime_site_packages()

    settings = get_settings()
    host = args.host or ("0.0.0.0" if args.server else "127.0.0.1")
    port = args.port or settings.server.port
    os.environ["VERBA_DESKTOP_MODE"] = "0" if args.server else "1"
    # the app logs this too, so the address also lands in the rotating file log
    os.environ["VERBA_BIND"] = f"{host}:{port}"

    from verba.config import data_dir

    sockets = bind_sockets(host, port)
    print(startup_banner(host, port, args.server, str(data_dir())), flush=True)

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
    # the sockets are already bound (see bind_sockets): desktop mode serves
    # the IPv4 and IPv6 loopback simultaneously
    server.run(sockets=sockets)


if __name__ == "__main__":
    main()
