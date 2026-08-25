"""First-run system checks and automatic installation of missing components.

Two kinds of installable components:
- ffmpeg: downloaded as a static build into <data>/tools (if not on PATH)
- Python feature groups: installed via pip into the running environment

All install steps report progress through the EventHub so the setup wizard
in the UI can show live status.

Feature-group modules are never imported by this module: on Windows a loaded
extension module (.pyd) is locked, and pip has to replace shared dependencies
(numpy, …) while installing later groups. Presence is therefore checked
through the import system's finders only, and the post-install smoke test runs
in a child process.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import invalidate_caches
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import httpx

from . import config
from .events import hub

logger = logging.getLogger(__name__)

MIN_PYTHON = (3, 11)
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024  # safety limit for tool downloads

# argv flags run.py handles before it boots the server: frozen builds have no
# interpreter to call, so the executable re-invokes itself for these helpers.
INTERNAL_PIP_FLAG = "--internal-pip"
INTERNAL_IMPORT_FLAG = "--internal-import"

# Windows: keep helper processes from flashing a console window
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FFMPEG_URLS = {
    "Windows": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "Linux": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
}


@dataclass
class FeatureGroup:
    key: str
    label: str
    packages: list[str]
    import_name: str
    required: bool = True


FEATURE_GROUPS: list[FeatureGroup] = [
    FeatureGroup(
        key="transcribe",
        label="Transkription (faster-whisper)",
        packages=["faster-whisper>=1.0"],
        import_name="faster_whisper",
    ),
    FeatureGroup(
        key="export",
        label="PDF-Export",
        packages=["fpdf2>=2.7"],
        import_name="fpdf",
    ),
    # LLM support needs no pip group: remote endpoints use the core httpx
    # client, local models use the llama.cpp binary (installed via settings).
    FeatureGroup(
        key="search",
        label="Semantische Suche",
        packages=["sentence-transformers>=3.0", "sqlite-vec>=0.1.6"],
        import_name="sentence_transformers",
        required=False,
    ),
]


@dataclass
class CheckResult:
    id: str
    label: str
    ok: bool
    required: bool
    installable: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SetupProgress:
    """In-memory state of the currently running setup, mirrored to the UI.

    `percent` is the progress over the whole setup, not over the current step:
    each pending component owns an equal slice of the bar, so it only ever
    moves forward. `checks` carries the checklist snapshot that goes with the
    current state, so the wizard can tick components off while it runs.
    """

    running: bool = False
    step: str = ""
    detail: str = ""
    percent: int = 0
    error: str = ""
    log: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    step_index: int = 0
    total_steps: int = 1

    def overall_percent(self, step_percent: int) -> int:
        """Blend the progress inside the current step into the overall bar."""
        total = max(self.total_steps, 1)
        done = min(max(self.step_index, 0), total)
        fraction = (done + min(max(step_percent, 0), 100) / 100) / total
        return min(int(fraction * 100), 100)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "step": self.step,
            "detail": self.detail,
            "percent": self.percent,
            "error": self.error,
            "log": self.log[-50:],
            "checks": self.checks,
        }


progress = SetupProgress()
_setup_lock = threading.Lock()


# ── individual checks ─────────────────────────────────────────────────


def check_python() -> CheckResult:
    ok = sys.version_info >= MIN_PYTHON
    return CheckResult(
        id="python",
        label="Python-Version",
        ok=ok,
        required=True,
        installable=False,
        detail=f"{platform.python_version()} ({sys.executable})",
    )


def ffmpeg_path() -> str | None:
    """ffmpeg from PATH, from settings (auto-installed), or from <data>/tools."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    configured = config.get_settings().setup.ffmpeg_path
    if configured and Path(configured).exists():
        return configured
    exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    for candidate in config.tools_dir().rglob(exe):
        return str(candidate)
    return None


def check_ffmpeg() -> CheckResult:
    path = ffmpeg_path()
    return CheckResult(
        id="ffmpeg",
        label="ffmpeg (Audio-Verarbeitung)",
        ok=path is not None,
        required=True,
        installable=platform.system() in FFMPEG_URLS,
        detail=path or "not found — will be installed automatically",
    )


def check_gpu() -> CheckResult:
    detail = "no NVIDIA GPU detected — transcription will run on the CPU"
    ok = False
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            detail = out.stdout.strip().splitlines()[0]
            ok = True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return CheckResult(
        id="gpu", label="GPU (optional)", ok=ok, required=False, installable=False, detail=detail
    )


def group_installed(group: FeatureGroup) -> bool:
    """Whether the group's top-level module can be located.

    Deliberately does not import it: importing pulls in binary dependencies
    (numpy via ctranslate2, …) which Windows then locks for the lifetime of
    the process, so a later pip run cannot replace them any more.

    An `origin` is required: a directory left behind by a half-deleted
    package resolves as a namespace package, which would look installed
    while its actual code is gone."""
    try:
        spec = find_spec(group.import_name)
    except (ImportError, ValueError):
        return False
    return spec is not None and spec.origin is not None


def check_groups() -> list[CheckResult]:
    results = []
    for group in FEATURE_GROUPS:
        ok = group_installed(group)
        results.append(
            CheckResult(
                id=f"group:{group.key}",
                label=group.label,
                ok=ok,
                required=group.required,
                installable=True,
                detail="installed" if ok else "will be installed during setup",
            )
        )
    return results


def _cpu_model() -> str:
    if platform.system() == "Windows":
        try:
            import winreg

            key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
        return os.environ.get("PROCESSOR_IDENTIFIER", "") or platform.processor()
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        for line in cpuinfo.splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def _memory_mb() -> tuple[int, int]:
    """(total, available) physical RAM in MB; (0, 0) if unknown."""
    try:
        if platform.system() == "Windows":
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            mb = 1024 * 1024
            return int(status.ullTotalPhys // mb), int(status.ullAvailPhys // mb)
        import re

        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        total = re.search(r"MemTotal:\s+(\d+) kB", meminfo)
        available = re.search(r"MemAvailable:\s+(\d+) kB", meminfo)
        return (
            int(total.group(1)) // 1024 if total else 0,
            int(available.group(1)) // 1024 if available else 0,
        )
    except (OSError, AttributeError, ValueError):
        return 0, 0


def _gpu_info() -> dict[str, Any]:
    """Name plus total/free VRAM in MB via nvidia-smi; empty values without one."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            name, total, free = out.stdout.strip().splitlines()[0].rsplit(",", 2)
            return {
                "name": name.strip(),
                "vram_total_mb": int(total.strip()),
                "vram_free_mb": int(free.strip()),
            }
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return {"name": "", "vram_total_mb": 0, "vram_free_mb": 0}


def system_info() -> dict[str, Any]:
    """General machine facts for the settings page (system section)."""
    ram_total, ram_available = _memory_mb()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "os_version": platform.version(),
        "python": platform.python_version(),
        "cpu_model": _cpu_model(),
        "cpu_cores": os.cpu_count() or 0,
        "ram_total_mb": ram_total,
        "ram_available_mb": ram_available,
        "gpu": _gpu_info(),
        "ffmpeg": check_ffmpeg().ok,
    }


def _all_checks() -> list[CheckResult]:
    return [check_python(), check_ffmpeg(), check_gpu(), *check_groups()]


def system_status() -> dict[str, Any]:
    checks = _all_checks()
    ready = all(c.ok for c in checks if c.required)
    settings = config.get_settings()
    return {
        "ready": ready,
        "setup_completed": settings.setup.completed,
        "checks": [c.as_dict() for c in checks],
        "setup": progress.as_dict(),
    }


# ── installation ──────────────────────────────────────────────────────


def _emit(step: str, step_percent: int, message: str | None = None) -> None:
    """Publish progress; `step_percent` is progress inside the current step."""
    progress.step = step
    progress.percent = progress.overall_percent(step_percent)
    if message:
        progress.detail = message
        progress.log.append(message)
        logger.info("Setup: %s", message)
    hub.publish("setup.progress", progress.as_dict())


def _refresh_checks() -> None:
    """Re-run the checks and push the snapshot so the wizard ticks off
    components as soon as they are installed."""
    progress.checks = [c.as_dict() for c in _all_checks()]
    hub.publish("setup.progress", progress.as_dict())


def _download(url: str, target: Path, on_percent: Callable[[int], None]) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        if total > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"Download zu groß ({total} Bytes): {url}")
        received = 0
        with open(target, "wb") as fh:
            for chunk in response.iter_bytes(chunk_size=1 << 16):
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"Download überschreitet Größenlimit: {url}")
                fh.write(chunk)
                if total:
                    on_percent(int(received * 100 / total))


def _extract_ffmpeg(archive: Path, dest: Path) -> Path:
    """Extract the archive and return the path of the ffmpeg binary inside dest."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    for candidate in dest.rglob(exe):
        if platform.system() != "Windows":
            candidate.chmod(0o755)
        return candidate
    raise RuntimeError("ffmpeg binary not found in archive")


def install_ffmpeg() -> str:
    """Download a static ffmpeg build into <data>/tools; returns the binary path."""
    system = platform.system()
    url = FFMPEG_URLS.get(system)
    if not url:
        raise RuntimeError(f"No automatic ffmpeg installation for {system}")

    _emit("ffmpeg", 0, f"Lade ffmpeg herunter ({url}) ...")
    dest = config.tools_dir() / "ffmpeg"
    dest.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if url.endswith(".zip") else ".tar.xz"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / f"ffmpeg{suffix}"
        # the download owns most of the step; extraction is the tail
        _download(url, archive, lambda p: _emit("ffmpeg", int(p * 0.9)))
        _emit("ffmpeg", 90, "Entpacke ffmpeg ...")
        binary = _extract_ffmpeg(archive, dest)

    settings = config.get_settings()
    settings.setup.ffmpeg_path = str(binary)
    config.save_settings(settings)
    _emit("ffmpeg", 100, f"ffmpeg installiert: {binary}")
    return str(binary)


def _run_child(cmd: list[str], on_line: Callable[[str], None]) -> tuple[int, list[str]]:
    """Run a helper process and stream its output line by line.

    The child writes into a file that the parent tails while it waits: windowed
    PyInstaller builds have no console, so a pipe on stdout is not reliable
    there, while an inherited file handle always is.
    """
    collected: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "output.log"
        with open(log, "w", encoding="utf-8") as sink:
            logger.info("running helper process: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdout=sink,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            seen = 0
            while True:
                done = proc.poll() is not None
                parts = log.read_text(encoding="utf-8", errors="replace").split("\n")
                # while the child runs, the last element may be a partial line
                complete = parts if done else parts[:-1]
                for line in complete[seen:]:
                    text = line.strip()
                    if text:
                        collected.append(text)
                        on_line(text)
                seen = len(complete)
                if done:
                    break
                time.sleep(0.25)
    return proc.returncode, collected


def _pip_command(packages: list[str]) -> list[str]:
    """pip invocation for the current build.

    Frozen builds have no `python -m pip`: the executable re-invokes itself
    (see run.py) and the bundled pip installs into <data>/site-packages, which
    run.py puts on sys.path at startup. Binary wheels only — source builds
    would need a real interpreter and a toolchain.
    """
    if config.FROZEN:
        target = config.runtime_site_packages()
        target.mkdir(parents=True, exist_ok=True)
        return [
            sys.executable,
            INTERNAL_PIP_FLAG,
            "install",
            "--progress-bar",
            "off",
            "--only-binary",
            ":all:",
            "--upgrade",
            "--target",
            str(target),
            *packages,
        ]
    return [sys.executable, "-m", "pip", "install", "--progress-bar", "off", *packages]


def _import_check_command(module: str) -> list[str]:
    if config.FROZEN:
        return [sys.executable, INTERNAL_IMPORT_FLAG, module]
    return [sys.executable, "-c", f"import {module}"]


_SITE_PACKAGES_PATH = re.compile(r"site-packages[\\/]+([A-Za-z0-9_.-]+)")


def _damaged_packages(output: list[str]) -> list[str]:
    """Top-level packages a failed pip run may have left half-deleted.

    pip names the file it could not replace; everything below site-packages
    belongs to the distribution named by the first path segment.
    """
    names: list[str] = []
    for line in output:
        for match in _SITE_PACKAGES_PATH.finditer(line):
            name = match.group(1).split(".")[0]
            if name and name not in names:
                names.append(name)
    return names


def _ensure_pip() -> None:
    """Source checkouts may run on a Python that ships without pip."""
    if config.FROZEN or find_spec("pip") is not None:
        return
    _emit("pip", 0, "pip fehlt in der Python-Umgebung; installiere pip ...")
    try:
        subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
        raise RuntimeError(f"pip konnte nicht eingerichtet werden: {detail.strip()}") from exc
    invalidate_caches()


_PIP_PROGRESS_PREFIXES = (
    "Collecting",
    "Downloading",
    "Using cached",
    "Installing",
    "Saved",
    "Successfully",
    "ERROR",
)


def _pip_install(packages: list[str], step: str) -> None:
    """Install packages in a child process, reporting pip's output live."""
    _ensure_pip()
    # pip's output is the only progress signal available; it arrives roughly
    # one line per package, so the step bar advances with the lines seen
    seen_lines = 0

    def on_line(line: str) -> None:
        nonlocal seen_lines
        if not line.startswith(_PIP_PROGRESS_PREFIXES):
            return
        seen_lines += 1
        _emit(step, min(15 + seen_lines * 4, 85), line)

    code, output = _run_child(_pip_command(packages), on_line)
    for line in output[-20:]:
        logger.info("pip: %s", line)
    if code != 0:
        damaged = _damaged_packages(output)
        if damaged:
            # Windows locked a loaded extension module mid-replacement — the
            # leftovers are removed at the next start so a retry is clean
            config.mark_site_packages_repair(damaged)
        detail = " | ".join(output[-3:])
        hint = " — bitte Verba neu starten und die Einrichtung erneut ausführen" if damaged else ""
        raise RuntimeError(f"pip-Installation fehlgeschlagen (Code {code}): {detail}{hint}")


def _verify_import(group: FeatureGroup) -> None:
    """Import the freshly installed module in a child process.

    Importing it here would lock its binary dependencies for the rest of this
    process' lifetime and break the pip run for the next group.
    """
    invalidate_caches()
    code, output = _run_child(_import_check_command(group.import_name), lambda line: None)
    if code != 0:
        detail = " | ".join(output[-3:]) or f"Code {code}"
        logger.error("import of %s failed after installation: %s", group.import_name, detail)
        raise RuntimeError(f"{group.label}: Import nach der Installation fehlgeschlagen: {detail}")


def install_group(group: FeatureGroup) -> None:
    """pip-install one feature group (frozen build: into <data>/site-packages)."""
    step = group.label
    package_list = ", ".join(group.packages)
    _emit(step, 0, f"Bereite Installation von {group.label} vor ({package_list}) ...")
    _emit(step, 10, f"Installiere {group.label} ...")
    _pip_install(group.packages, step)
    _emit(step, 90, f"Prüfe {group.label} ...")
    _verify_import(group)
    _emit(step, 100, f"{group.label} installiert und geprüft.")


def _pending_steps(include_optional: bool) -> list[tuple[str, Callable[[], Any]]]:
    """Everything this setup run has to do, in order — one bar slice each."""
    steps: list[tuple[str, Callable[[], Any]]] = []
    if not check_ffmpeg().ok:
        steps.append(("ffmpeg", install_ffmpeg))
    for group in FEATURE_GROUPS:
        if not (group.required or include_optional):
            continue
        if not group_installed(group):
            steps.append((group.label, lambda bound=group: install_group(bound)))
    return steps


def run_setup(include_optional: bool = True) -> None:
    """Run all pending installations sequentially (call from a worker thread)."""
    if not _setup_lock.acquire(blocking=False):
        return
    progress.running = True
    progress.error = ""
    progress.log.clear()
    progress.percent = 0
    progress.step_index = 0
    steps = _pending_steps(include_optional)
    progress.total_steps = max(len(steps), 1)
    _refresh_checks()
    try:
        for index, (label, action) in enumerate(steps):
            progress.step_index = index
            _emit(label, 0)
            action()
            progress.step_index = index + 1
            _refresh_checks()

        settings = config.get_settings()
        settings.setup.completed = True
        config.save_settings(settings)
        progress.step_index = progress.total_steps
        _emit("done", 100, "Alle Komponenten installiert und geprüft. Einrichtung abgeschlossen.")
    except Exception as exc:
        logger.exception("setup failed")
        progress.error = str(exc)
        progress.step = "error"
        progress.detail = f"Fehler: {exc}"
        progress.log.append(progress.detail)  # the bar keeps the reached value
    finally:
        progress.running = False
        _refresh_checks()  # publishes the final state, including the checklist
        _setup_lock.release()
