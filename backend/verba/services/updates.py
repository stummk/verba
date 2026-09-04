"""Updating Verba itself from the GitHub releases of this repository.

The release pipeline publishes exactly three artifacts, and each one is the
update path for the installation it came from:

- ``Verba-Setup-<version>.exe``       Windows: the installer runs silently,
                                      replaces the installation, closes Verba
                                      and starts it again
- ``Verba-<version>-x86_64.AppImage`` Linux desktop: replaces the AppImage
                                      that is running and relaunches it
- ``verba-server-<version>.zip``      Linux server: refreshes the virtualenv,
                                      replaces the application files and asks
                                      the service manager for a restart

A source checkout is not updated from here — that one belongs to git.

The version of the running app comes from ``verba/__init__.py``, which the
release pipeline stamps with the release tag (packaging/stamp_version.py), so
comparing it against the newest tag is the entire check. It is cached and
repeated once a day, and it can be switched off (``updates.check_enabled``) —
then nothing ever leaves the machine.

Every step of an installation is written to a log the settings page shows
live (event ``update.progress``). It lives in this process only: an update
ends in a restart, and the new version starts with an empty log — what it
would have to say is that it is the new version, which the page shows anyway.

An update removes the version it replaces. Nothing of a release is worth
keeping once its successor runs, and nothing that matters is part of one: the
database, the logs, settings.json, the workspaces and the models all live
outside the application files.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from .. import __version__, config, lifecycle, procutil
from ..events import hub
from . import download

logger = logging.getLogger(__name__)

REPOSITORY = "stummk/verba"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"

#: Installer, AppImage and server package are each well under this.
MAX_ASSET_BYTES = 600 * 1024 * 1024
#: How long a check result is reused before GitHub is asked again.
CHECK_TTL_S = 6 * 3600
#: How often the running app looks for a new release.
CHECK_INTERVAL_S = 24 * 3600
#: The start has enough to do — the first check waits.
FIRST_CHECK_DELAY_S = 20.0
#: Exit code that tells a service manager to start the new version.
EXIT_RESTART = 42
#: How long the Windows installer may run before we stop waiting for it — it
#: closes this process itself once it reaches our files, so that is no error.
INSTALLER_TIMEOUT_S = 900
#: Refreshing the virtualenv of a server installation.
PIP_TIMEOUT_S = 900
_LOG_LINES = 300
#: Lines of foreign output (pip, installer log) that reach our own log.
_OUTPUT_TAIL = 20

# ── installation kinds ────────────────────────────────────────────────

WINDOWS_INSTALLER = "windows-installer"
APPIMAGE = "appimage"
SERVER_ZIP = "server-zip"
SOURCE = "source"
UNSUPPORTED = "unsupported"

#: The kinds this module can actually replace.
INSTALLABLE = (WINDOWS_INSTALLER, APPIMAGE, SERVER_ZIP)

#: What a server package brings along — the same list deploy/install.sh
#: replaces, so runtime data, the virtualenv and the workspaces stay put.
PACKAGE_ITEMS = (
    "backend",
    "frontend",
    "docs",
    "requirements",
    "deploy",
    "run.py",
    "start.sh",
    "README.md",
)

#: The only architecture an AppImage is built for.
_X86_64 = {"x86_64", "amd64", "x64"}

#: An asset name from the network is only ever used as a file name.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

RESTART_NONE = ""
#: Start this command once the server has stopped (desktop installations).
RESTART_EXEC = "exec"
#: Exit with EXIT_RESTART and let the service manager start the new version.
RESTART_EXIT = "exit"

_KIND_LABELS = {
    WINDOWS_INSTALLER: "Windows-Installation",
    APPIMAGE: "Linux-AppImage",
    SERVER_ZIP: "Server-Installation",
}


def appimage_path() -> Path | None:
    """The AppImage file the user launched, if this is an AppImage run.

    The AppImage runtime exports APPIMAGE with the absolute path of the file
    itself — that is the file an update has to replace.
    """
    raw = os.environ.get("APPIMAGE", "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def installation_kind() -> str:
    """Which of the three release artifacts this installation came from."""
    if config.FROZEN:
        if platform.system() == "Windows":
            return WINDOWS_INSTALLER
        if appimage_path() is not None:
            return APPIMAGE
        return UNSUPPORTED
    if (config.PROJECT_ROOT / ".git").exists():
        return SOURCE
    return SERVER_ZIP


def installable(kind: str | None = None) -> tuple[bool, str]:
    """Whether an update can be installed here, and why not if it cannot.

    The reason is shown in the settings page, so it says what to do instead.
    """
    kind = kind or installation_kind()
    if kind == SOURCE:
        return False, "Quellcode-Installation — Updates kommen hier über git."
    if kind == UNSUPPORTED:
        return False, (
            f"Für diese Installation ({platform.system()} {platform.machine()}) "
            "gibt es kein automatisches Update."
        )
    if kind == APPIMAGE:
        target = appimage_path()
        if target is None:
            return False, "Die AppImage-Datei ist nicht auffindbar."
        if platform.machine().lower() not in _X86_64:
            return False, f"Für {platform.machine()} wird kein AppImage gebaut."
        if not os.access(target.parent, os.W_OK):
            return False, f"Keine Schreibrechte auf {target.parent}."
    if kind == SERVER_ZIP and not os.access(config.PROJECT_ROOT, os.W_OK):
        return False, f"Keine Schreibrechte auf {config.PROJECT_ROOT}."
    return True, ""


# ── version comparison ────────────────────────────────────────────────

_NUMBERS = re.compile(r"(\d+(?:\.\d+)*)(.*)")


def parse_version(text: str) -> tuple[tuple[int, ...], str]:
    """Split a version into its numbers and whatever follows them.

    Release tags are written as "0.1.1" or "v0.1.1"; a pre-release carries a
    suffix ("1.0.0-rc1") that only matters when the numbers are equal.
    """
    cleaned = (text or "").strip()
    if cleaned[:1] in ("v", "V"):
        cleaned = cleaned[1:]
    match = _NUMBERS.match(cleaned)
    if not match:
        return (), cleaned
    return tuple(int(part) for part in match.group(1).split(".")), match.group(2).strip(" .-")


def is_newer(candidate: str, current: str) -> bool:
    """Whether `candidate` is a later version than `current`."""
    new_numbers, new_suffix = parse_version(candidate)
    old_numbers, old_suffix = parse_version(current)
    if not new_numbers:
        return False
    width = max(len(new_numbers), len(old_numbers))

    def padded(numbers: tuple[int, ...]) -> tuple[int, ...]:
        return numbers + (0,) * (width - len(numbers))

    if padded(new_numbers) != padded(old_numbers):
        return padded(new_numbers) > padded(old_numbers)
    # same numbers: the finished release beats the pre-release of it
    return bool(old_suffix) and not new_suffix


# ── the check ─────────────────────────────────────────────────────────

_check_lock = threading.Lock()
_check: dict[str, Any] = {
    "checked_at": 0.0,
    "version": "",
    "url": "",
    "notes": "",
    "asset": None,
    "error": "",
}


def _get_json(url: str) -> Any:
    response = httpx.get(
        url,
        timeout=30,
        follow_redirects=True,
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    return response.json()


def _asset_for(kind: str, assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The release asset that updates this kind of installation."""

    def matches(name: str) -> bool:
        lowered = name.lower()
        if kind == WINDOWS_INSTALLER:
            return lowered.startswith("verba-setup") and lowered.endswith(".exe")
        if kind == APPIMAGE:
            return lowered.endswith(".appimage") and "x86_64" in lowered
        if kind == SERVER_ZIP:
            return lowered.startswith("verba-server") and lowered.endswith(".zip")
        return False

    for asset in assets:
        name = str(asset.get("name") or "")
        if matches(name) and _SAFE_NAME.match(name) and asset.get("browser_download_url"):
            return asset
    return None


def check(force: bool = False) -> dict[str, Any]:
    """Ask GitHub for the newest release; cached for CHECK_TTL_S.

    Never raises: a machine without internet access, a rate limit or a
    firewall in between is not something the user has to act on, so the
    failure becomes part of the result and the app carries on.
    """
    kind = installation_kind()
    with _check_lock:
        age = time.time() - float(_check["checked_at"])
        fresh = bool(_check["checked_at"]) and age < CHECK_TTL_S and not _check["error"]
    if fresh and not force:
        return info()

    try:
        release = _get_json(LATEST_RELEASE_API)
    except Exception as exc:  # noqa: BLE001 — every failure means "unknown"
        logger.warning("update check failed: %s", exc)
        with _check_lock:
            _check.update(checked_at=time.time(), error=download.error_message(exc))
        return info()

    numbers, suffix = parse_version(str(release.get("tag_name") or ""))
    version = ".".join(str(number) for number in numbers)
    if version and suffix:
        version = f"{version}-{suffix}"
    with _check_lock:
        _check.update(
            checked_at=time.time(),
            version=version,
            url=str(release.get("html_url") or RELEASES_PAGE),
            notes=str(release.get("body") or "")[:2000],
            asset=_asset_for(kind, release.get("assets") or []),
            error="",
        )
    logger.info("update check: running %s, newest release %s", __version__, version or "?")
    return info()


def info() -> dict[str, Any]:
    """Everything the settings page needs: the check, and the installation."""
    kind = installation_kind()
    ok, reason = installable(kind)
    with _check_lock:
        latest = str(_check["version"])
        asset = _check["asset"]
        checked = float(_check["checked_at"])
        url = str(_check["url"]) or RELEASES_PAGE
        notes = str(_check["notes"])
        error = str(_check["error"])
    available = bool(latest) and is_newer(latest, __version__)
    if available and ok and asset is None:
        ok, reason = False, f"Release {latest} enthält kein Paket für diese Installation."
    return {
        "current": __version__,
        "latest": latest,
        "available": available,
        "kind": kind,
        "supported": ok,
        "reason": reason,
        "can_install": bool(available and ok and asset is not None),
        "url": url,
        "notes": notes,
        "checked": bool(checked),
        "error": error,
        "install": state(),
    }


def summary() -> dict[str, Any]:
    """The two facts /api/system/status carries — never a network call."""
    with _check_lock:
        latest = str(_check["version"])
    return {
        "update_available": bool(latest) and is_newer(latest, __version__),
        "update_version": latest,
    }


_checker: threading.Thread | None = None


def start_background_checks() -> bool:
    """Look for a new release now and then; announces one via the event hub.

    Skipped for a source checkout: git already tells that story, and nothing
    here could install it anyway.
    """
    global _checker
    if _checker is not None and _checker.is_alive():
        return False
    if installation_kind() not in INSTALLABLE:
        return False
    if not config.get_settings().updates.check_enabled:
        return False
    _checker = threading.Thread(target=_check_loop, daemon=True, name="update-check")
    _checker.start()
    return True


def _check_loop() -> None:
    delay = FIRST_CHECK_DELAY_S
    while True:
        time.sleep(delay)
        delay = CHECK_INTERVAL_S
        if not config.get_settings().updates.check_enabled:
            continue
        data = check(force=True)
        if data["available"]:
            hub.publish(
                "update.available",
                {
                    "version": data["latest"],
                    "current": data["current"],
                    "can_install": data["can_install"],
                },
            )


# ── installation state ────────────────────────────────────────────────

_install_lock = threading.Lock()
_install: dict[str, Any] = {
    "running": False,
    "percent": 0,
    "detail": "",
    "error": "",
    "log": [],
    "version": "",
    "finished_at": 0.0,
}
_restart: dict[str, Any] = {"mode": RESTART_NONE, "command": []}


def download_dir() -> Path:
    """Where update artifacts land — re-downloadable, like the tools."""
    path = config.tools_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_downloads() -> None:
    """Throw away what an update left behind — called at every start.

    A Windows update ends with this process being closed by the installer, so
    the installer and its log are still lying there when the new version comes
    up. Nothing of it is needed any more: the log lives in the app only while
    the installation runs.
    """
    directory = config.tools_dir() / "updates"
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def _installer_log() -> Path:
    return download_dir() / "installer.log"


def _installer_log_tail() -> list[str]:
    """The end of the Windows installer's own log.

    Only read when the installer refused to do its job: it says why in its own
    words, and this process is still alive to pass that on.
    """
    try:
        lines = _installer_log().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    tail = [line.strip() for line in lines if line.strip()][-_OUTPUT_TAIL:]
    return [f"Installer: {line}" for line in tail]


def state() -> dict[str, Any]:
    """Snapshot of the running (or last) installation, log included."""
    with _install_lock:
        return {**_install, "log": list(_install["log"])}


def _emit(percent: int, message: str, state: str = "running") -> None:
    """Record one step and broadcast it together with the log so far."""
    with _install_lock:
        log = _install["log"]
        if message and (not log or log[-1] != message):
            log.append(message)
            del log[:-_LOG_LINES]
        _install["percent"] = percent
        _install["running"] = state == "running"
        if message:
            _install["detail"] = message
        if state == "error":
            _install["error"] = message
        if state != "running":
            _install["finished_at"] = time.time()
        snapshot = {**_install, "log": list(log)}
    if message:
        logger.info("update: %s", message)
    hub.publish("update.progress", {**snapshot, "state": state})


# ── the update itself ─────────────────────────────────────────────────


def start_update() -> dict[str, Any]:
    """Install the newest release in a background thread (API entry point)."""
    data = check()
    if _install["running"]:
        return {"started": False, "reason": "Die Aktualisierung läuft bereits."}
    if not data["available"]:
        return {"started": False, "reason": "Verba ist auf dem neuesten Stand."}
    if not data["can_install"]:
        return {"started": False, "reason": data["reason"] or "Update hier nicht möglich."}
    with _check_lock:
        asset = dict(_check["asset"] or {})
    kind, version = data["kind"], data["latest"]

    with _install_lock:
        _install.update(
            running=True,
            percent=0,
            detail="",
            error="",
            log=[],
            version=version,
            finished_at=0.0,
        )
    threading.Thread(
        target=_run_update,
        args=(kind, version, asset),
        daemon=True,
        name="update-install",
    ).start()
    return {"started": True, "reason": ""}


def _run_update(kind: str, version: str, asset: dict[str, Any]) -> None:
    try:
        _emit(1, f"Aktualisierung von Version {__version__} auf {version}")
        _emit(2, f"Installationsart: {_KIND_LABELS.get(kind, kind)}")
        artifact = _download_asset(asset)
        if kind == WINDOWS_INSTALLER:
            _install_windows(artifact, version)
        elif kind == APPIMAGE:
            _install_appimage(artifact, version)
        else:
            _install_server_package(artifact, version)
    except Exception as exc:  # noqa: BLE001 — the message is the user's answer
        logger.exception("update to %s failed", version)
        _emit(0, download.error_message(exc), state="error")
    finally:
        with _install_lock:
            _install["running"] = False


def _download_asset(asset: dict[str, Any]) -> Path:
    """Fetch the release artifact into a cleaned-out download directory."""
    name = str(asset.get("name") or "")
    if not _SAFE_NAME.match(name):
        raise RuntimeError(f"Unerwarteter Dateiname im Release: {name!r}")
    directory = download_dir()
    # leftovers of an earlier attempt cannot be verified, and the installer
    # log of the last run has been read into our own log long before this
    for path in directory.iterdir():
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)

    size = int(asset.get("size") or 0)
    if size > MAX_ASSET_BYTES:
        raise RuntimeError(f"Das Paket ist zu groß ({size} Bytes)")
    # the artifact plus what it unpacks to
    download.require_free_space(directory, size * 3)
    target = directory / name
    _emit(3, f"Lade {name} ({size // (1024 * 1024)} MB) ...")
    download.fetch(
        str(asset["browser_download_url"]),
        target,
        MAX_ASSET_BYTES,
        download.phase(_emit, 3, 67, f"Lade {name} ..."),
    )
    _emit(70, f"Geladen: {target}")
    return target


def _verify_magic(path: Path, magic: bytes, label: str) -> None:
    """A download that is not what it claims to be must never be installed.

    An HTML error page saved under the asset's name is the realistic case: a
    proxy login screen, a rate limit, a release that moved.
    """
    with open(path, "rb") as handle:
        head = handle.read(len(magic))
    if head != magic:
        raise RuntimeError(f"Die geladene Datei ist kein {label}")


def _install_windows(installer: Path, version: str) -> None:
    """Hand over to the Inno Setup installer, which closes and restarts us.

    `/SILENT` shows a progress window instead of the wizard, and
    `/FORCECLOSEAPPLICATIONS` closes Verba — its files are the ones being
    replaced, and this process has no window a polite close request could
    reach. Bringing the app back is the installer's `[Run]` entry for a silent
    run (packaging/verba.iss), which is why the restart manager is told not to
    restart anything: exactly one of the two starts Verba again.

    The installer writes into Program Files, so Windows asks for confirmation
    once; that dialog is the only thing left to do. If it is declined the
    installer ends with 1223, this process stays alive and the log says so.
    """
    _verify_magic(installer, b"MZ", "Windows-Installer")
    command = [
        str(installer),
        "/SILENT",
        "/NORESTART",
        "/SUPPRESSMSGBOXES",
        "/FORCECLOSEAPPLICATIONS",
        f"/LOG={_installer_log()}",
    ]
    _emit(75, "Starte den Installer — bitte die Rechteanfrage von Windows bestätigen")
    process = procutil.popen(command, close_fds=True)
    _emit(80, "Der Installer ersetzt die Installation; Verba schließt und startet neu")
    try:
        code = process.wait(timeout=INSTALLER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # nothing went wrong: the installer is simply slower than we wait, and
        # it ends this process itself as soon as it reaches our files
        _emit(90, "Der Installer läuft weiter — Verba schließt sich, sobald er soweit ist")
        return
    if code != 0:
        raise RuntimeError(_installer_error(code))
    # still alive: the installer did not have to close us after all
    _emit(98, f"Version {version} installiert")
    _finish(version, RESTART_EXEC, [sys.executable])


def _installer_error(code: int) -> str:
    if code == 1223:
        return "Die Rechteanfrage von Windows wurde abgelehnt — Update nicht installiert"
    if code in (2, 5):
        return "Der Installer wurde abgebrochen — Update nicht installiert"
    tail = " ".join(_installer_log_tail()[-3:])
    return f"Der Installer endete mit Code {code}. {tail}".strip()


def _install_appimage(downloaded: Path, version: str) -> None:
    """Replace the running AppImage file and relaunch it.

    The new image is written next to the old one and only then renamed over
    it, so a download that fails halfway cannot leave a file that no longer
    starts. The old version is gone afterwards — one file is the whole
    installation, and the running process keeps its own copy open until it
    exits.
    """
    target = appimage_path()
    if target is None:
        raise RuntimeError("Die AppImage-Datei ist nicht auffindbar")
    _verify_magic(downloaded, b"\x7fELF", "AppImage")
    download.require_free_space(target.parent, downloaded.stat().st_size * 2)
    staged = target.with_name(target.name + ".new")
    _emit(75, f"Schreibe die neue AppImage nach {staged}")
    shutil.copy2(downloaded, staged)
    staged.chmod(0o755)
    os.replace(staged, target)
    _emit(95, f"Die vorherige Version wurde ersetzt: {target}")
    _emit(98, f"Version {version} installiert")
    _finish(version, RESTART_EXEC, [str(target)])


def _install_server_package(archive: Path, version: str) -> None:
    """Replace the application files of a server installation.

    The order matters: the dependencies are installed first, because a pip run
    that fails must not leave half-replaced application files behind. Only
    PACKAGE_ITEMS is touched — the database, the logs, settings.json, the
    workspaces and the virtualenv are not part of a release and stay where
    they are. The old application files are removed, one item at a time and
    only once its replacement stands.
    """
    if not zipfile.is_zipfile(archive):
        raise RuntimeError("Das geladene Serverpaket ist kein ZIP-Archiv")
    root = config.PROJECT_ROOT
    staging = download_dir() / "unpacked"
    _emit(72, "Entpacke das Serverpaket ...")
    with zipfile.ZipFile(archive) as bundle:
        _reject_unsafe_members(bundle)
        bundle.extractall(staging)
    source = _package_root(staging)
    missing = [item for item in PACKAGE_ITEMS if not (source / item).exists()]
    if missing:
        raise RuntimeError(f"Im Serverpaket fehlt: {', '.join(missing)}")

    _emit(78, "Aktualisiere die Python-Abhängigkeiten ...")
    _pip_install(source / "requirements" / "core.txt")

    _emit(90, f"Ersetze die Anwendungsdateien in {root}")
    for item in PACKAGE_ITEMS:
        _replace(root / item, source / item)
    _emit(96, "Die Dateien der vorherigen Version sind entfernt")
    _cleanup(staging, archive)
    _emit(98, f"Version {version} installiert")

    if os.environ.get("INVOCATION_ID"):  # started by systemd
        _finish(version, RESTART_EXIT, [])
        return
    _emit(100, f"Version {version} installiert — bitte Verba neu starten", state="done")


def _replace(current: Path, fresh: Path) -> None:
    """Put `fresh` where `current` is and delete what was there.

    The old item is renamed aside first, not deleted: a copy that fails then
    still has something to put back, and the rename happens inside the same
    directory, which no filesystem can fail to do halfway.
    """
    aside = current.with_name(current.name + ".replaced")
    _remove(aside)
    if current.exists():
        os.replace(current, aside)
    try:
        shutil.copytree(fresh, current) if fresh.is_dir() else shutil.copy2(fresh, current)
    except OSError:
        _remove(current)
        if aside.exists():
            os.replace(aside, current)
        raise
    _remove(aside)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _cleanup(*paths: Path) -> None:
    """Remove what the installation does not need any more."""
    for path in paths:
        _remove(path)


def _reject_unsafe_members(bundle: zipfile.ZipFile) -> None:
    """An archive from the network never writes outside its target directory."""
    for name in bundle.namelist():
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
            raise RuntimeError(f"Unerwarteter Pfad im Serverpaket: {name}")


def _package_root(staging: Path) -> Path:
    """The single `verba-server-<version>` directory inside the archive."""
    entries = [path for path in staging.iterdir() if path.is_dir()]
    if len(entries) == 1 and not (staging / "run.py").exists():
        return entries[0]
    return staging


def _pip_install(requirements: Path) -> None:
    """Install the new core requirements into the environment we run in.

    `sys.executable` is the virtualenv's interpreter for a service installed
    by deploy/install.sh, so this refreshes exactly that environment.
    """
    result = procutil.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "-r", str(requirements)],
        capture_output=True,
        text=True,
        timeout=PIP_TIMEOUT_S,
    )
    for line in [line.strip() for line in (result.stdout or "").splitlines() if line.strip()][
        -_OUTPUT_TAIL:
    ]:
        _emit(85, f"pip: {line}")
    if result.returncode != 0:
        errors = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        raise RuntimeError(
            "Die Abhängigkeiten ließen sich nicht installieren: "
            + (" ".join(errors[-3:]) or f"pip endete mit Code {result.returncode}")
        )


def _finish(version: str, mode: str, command: list[str]) -> None:
    """Announce the finished installation and stop the app so it comes back."""
    with _install_lock:
        _restart.update(mode=mode, command=list(command))
    message = (
        f"Version {version} installiert — Verba startet neu"
        if mode == RESTART_EXEC
        else f"Version {version} installiert — der Dienst startet neu"
    )
    _emit(100, message, state="done")
    # the UI has the closing message; a moment later the process goes down
    lifecycle.stop_process(1.5)


def pending_restart() -> tuple[str, list[str]]:
    """What run.py has to do once the server has stopped."""
    with _install_lock:
        return str(_restart["mode"]), list(_restart["command"])


def finish_pending_restart() -> None:
    """Bring the freshly installed version up — after the server has stopped.

    At that point the port is free and nothing of the old version is serving
    any more, which is why the relaunch belongs here and not into the request
    that started the update.
    """
    mode, command = pending_restart()
    if mode == RESTART_EXEC and command:
        logger.info("restarting into the updated version: %s", command[0])
        kwargs: dict[str, Any] = {"close_fds": True}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        procutil.popen(command, **kwargs)
        return
    if mode == RESTART_EXIT:
        logger.info("exiting with %d so the service manager starts the new version", EXIT_RESTART)
        sys.exit(EXIT_RESTART)
