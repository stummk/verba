"""Updating the Linux server Verba runs on — its packages, not Verba itself.

On a headless installation Verba is often the only thing anybody opens on that
machine, while its security updates come from the distribution. So the
settings page offers exactly what an administrator would type in over SSH:

    apt-get update
    apt-get --yes upgrade

Nothing more: no ``dist-upgrade`` (that one may remove packages), no
``autoremove``, no reboot — an upgrade that needs one says so and leaves the
decision to the administrator. Both commands run non-interactively and keep
the configuration files that are on the machine, and every line they say goes
into a log the page shows live (event ``system.upgrade``). That log lives in
this process only: it is the record of one action somebody watched happen.

Offered only where it applies. A Windows installation has no apt, and a
desktop installation is not somebody's server — there the operating system is
updated by the person sitting in front of it.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import lifecycle, procutil
from ..events import hub
from . import updates

logger = logging.getLogger(__name__)

#: The scriptable front end; `apt` itself has no stable command line.
APT = "apt-get"
#: Reading the package lists is a handful of HTTP requests.
UPDATE_TIMEOUT_S = 600
#: Installing them can mean a lot of packages on a server left alone for long.
UPGRADE_TIMEOUT_S = 3600
_LOG_LINES = 400

#: Debian and Ubuntu leave this behind when a new kernel or libc landed.
REBOOT_MARKER = Path("/var/run/reboot-required")

#: Nothing may wait for an answer: keep the configuration that is installed.
_KEEP_CONFIG = (
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
)


def supported() -> bool:
    """Whether this installation is a Linux server — where apt is our business.

    The AppImage and the Windows installer are desktop installations, and
    desktop mode is a local app started by a double click. What is left is the
    server: the zip installation from deploy/install.sh and a checkout started
    with ``--server``.
    """
    if platform.system() != "Linux":
        return False
    if lifecycle.desktop_mode():
        return False
    return updates.installation_kind() != updates.APPIMAGE


def _elevation() -> list[str] | None:
    """The prefix that gets us root, or None if there is no way to get there.

    The systemd unit runs as the "verba" user, so installing packages needs
    sudo — and it has to work without a password, because there is nobody at a
    terminal to type one. `sudo -n true` is the only reliable way to ask.
    """
    if getattr(os, "geteuid", lambda: 1)() == 0:
        return []
    if shutil.which("sudo") is None:
        return None
    try:
        result = procutil.run(["sudo", "-n", "true"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return ["sudo", "-n"] if result.returncode == 0 else None


def ready() -> tuple[bool, str]:
    """Whether an update can be started here, and why not if it cannot.

    The reason is shown under the button, so it says what would have to change.
    """
    if not supported():
        return False, "Systempakete werden nur auf einem Linux-Server aktualisiert."
    if shutil.which(APT) is None:
        return False, (
            "Dieser Server hat kein apt — die Systempakete kommen hier über die "
            "Paketverwaltung der Distribution."
        )
    if _elevation() is None:
        return False, (
            "Verba darf keine Pakete installieren. Dafür müsste der Dienst als root laufen "
            "oder sudo ohne Passwort erlaubt sein."
        )
    return True, ""


# ── state ─────────────────────────────────────────────────────────────

_lock = threading.Lock()
_run: dict[str, Any] = {
    "running": False,
    "detail": "",
    "error": "",
    "log": [],
    "reboot": False,
    "finished_at": 0.0,
}


def state() -> dict[str, Any]:
    """Snapshot of the running (or last) update, log included."""
    with _lock:
        return {**_run, "log": list(_run["log"])}


def info() -> dict[str, Any]:
    """Everything the settings page needs to draw the server row."""
    ok, reason = ready()
    current = state()
    return {
        "supported": supported(),
        "can_run": bool(ok and not current["running"]),
        "reason": reason,
        "run": current,
    }


def _emit(message: str, state: str = "running") -> None:
    """Add one line to the log and broadcast the whole log with it."""
    with _lock:
        log = _run["log"]
        if message:
            log.append(message)
            del log[:-_LOG_LINES]
            _run["detail"] = message
        _run["running"] = state == "running"
        if state == "error":
            _run["error"] = message
        if state != "running":
            _run["finished_at"] = time.time()
        snapshot = {**_run, "log": list(log)}
    if message:
        logger.info("server update: %s", message)
    hub.publish("system.upgrade", {**snapshot, "state": state})


# ── the update itself ─────────────────────────────────────────────────


def start() -> dict[str, Any]:
    """Update the system packages in a background thread (API entry point)."""
    ok, reason = ready()
    if not ok:
        return {"started": False, "reason": reason}
    with _lock:
        if _run["running"]:
            return {"started": False, "reason": "Die Serveraktualisierung läuft bereits."}
        _run.update(running=True, detail="", error="", log=[], reboot=False, finished_at=0.0)
    threading.Thread(target=_upgrade, daemon=True, name="server-update").start()
    return {"started": True, "reason": ""}


def _upgrade() -> None:
    elevate = _elevation() or []
    try:
        _emit("apt-get update — die Paketlisten werden gelesen")
        _apt([*elevate, APT, "update"], UPDATE_TIMEOUT_S)
        _emit("apt-get upgrade — die Pakete werden installiert")
        _apt([*elevate, APT, "--yes", *_KEEP_CONFIG, "upgrade"], UPGRADE_TIMEOUT_S)
        reboot = REBOOT_MARKER.exists()
        with _lock:
            _run["reboot"] = reboot
        _emit(
            "Fertig — der Server muss noch neu gestartet werden."
            if reboot
            else "Fertig — die Systempakete sind aktuell.",
            state="done",
        )
    except Exception as exc:  # noqa: BLE001 — the message is the user's answer
        logger.exception("server update failed")
        _emit(_error_message(exc), state="error")
    finally:
        with _lock:
            _run["running"] = False


def _apt(command: list[str], timeout: float) -> None:
    """Run one apt command and put every line it says into the log.

    The output is read line by line instead of collected, because that is the
    whole point of the button: an administrator watches what happens on the
    machine. apt has no answer to "how long will this take", so the only guard
    is a timer that ends a run which cannot finish.
    """
    process = procutil.popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_environment(),
    )
    watchdog = threading.Timer(timeout, process.kill)
    watchdog.start()
    try:
        for raw in process.stdout or ():
            # apt writes progress by overwriting a line; of such a line only
            # the last state it reached is worth keeping
            line = next((part.strip() for part in reversed(raw.split("\r")) if part.strip()), "")
            if line:
                _emit(line)
        code = process.wait()
    finally:
        watchdog.cancel()
    if code != 0:
        raise RuntimeError(f"{APT} {command[-1]} endete mit Code {code}")


def _environment() -> dict[str, str]:
    """Non-interactive, and English: this log is read by an administrator."""
    return {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"}


def _error_message(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"{APT} ist auf diesem Server nicht vorhanden."
    if isinstance(exc, PermissionError):
        return "Verba darf keine Pakete installieren."
    return str(exc) or exc.__class__.__name__
