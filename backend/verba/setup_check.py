"""First-run system checks and automatic installation of missing components.

Two kinds of installable components:
- ffmpeg: downloaded as a static build into <data>/tools (if not on PATH)
- Python feature groups: installed via pip into the running environment

All install steps report progress through the EventHub so the setup wizard
in the UI can show live status.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module, invalidate_caches
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import httpx

from . import config
from .events import hub

logger = logging.getLogger(__name__)

MIN_PYTHON = (3, 11)
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024  # safety limit for tool downloads

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
    """In-memory state of the currently running setup, mirrored to the UI."""

    running: bool = False
    step: str = ""
    detail: str = ""
    percent: int = 0
    error: str = ""
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "step": self.step,
            "detail": self.detail,
            "percent": self.percent,
            "error": self.error,
            "log": self.log[-50:],
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
    try:
        import_module(group.import_name)
        return True
    except ImportError:
        return False


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


def system_status() -> dict[str, Any]:
    checks = [check_python(), check_ffmpeg(), check_gpu(), *check_groups()]
    ready = all(c.ok for c in checks if c.required)
    settings = config.get_settings()
    return {
        "ready": ready,
        "setup_completed": settings.setup.completed,
        "checks": [c.as_dict() for c in checks],
        "setup": progress.as_dict(),
    }


# ── installation ──────────────────────────────────────────────────────


def _emit(step: str, percent: int, message: str | None = None) -> None:
    progress.step = step
    progress.percent = percent
    if message:
        progress.detail = message
        progress.log.append(message)
        logger.info("Setup: %s", message)
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
        _download(url, archive, lambda p: _emit("ffmpeg", p))
        _emit("ffmpeg", 100, "Entpacke ffmpeg ...")
        binary = _extract_ffmpeg(archive, dest)

    settings = config.get_settings()
    settings.setup.ffmpeg_path = str(binary)
    config.save_settings(settings)
    _emit("ffmpeg", 100, f"ffmpeg installiert: {binary}")
    return str(binary)


def _pip_install_frozen(packages: list[str]) -> None:
    """PyInstaller builds have no usable `sys.executable -m pip` — run the
    bundled pip in-process and install into <data>/site-packages (which
    run.py put on sys.path at startup). Binary wheels only: source builds
    would need a real Python interpreter.

    pip is bundled as a plain file tree (<bundle>/pip-lib), not as frozen
    modules: its vendored distlib resolves resources only through standard
    path-based importers."""
    import contextlib
    import io

    pip_lib = config.bundle_root() / "pip-lib"
    if pip_lib.is_dir() and str(pip_lib) not in sys.path:
        sys.path.insert(0, str(pip_lib))
    target = config.data_dir() / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    from pip._internal.cli.main import main as pip_main

    # windowed builds have no stdout — capture pip's output for the log
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = pip_main(
            [
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
        )
    tail = buffer.getvalue().strip().splitlines()[-20:]
    for line in tail:
        logger.info("pip: %s", line)
    if code != 0:
        # a failed in-process install may leave locked, half-deleted packages —
        # mark site-packages for a clean rebuild at the next start
        config.site_packages_repair_marker().touch()
        detail = " | ".join(line.strip() for line in tail[-3:])
        raise RuntimeError(
            f"pip installation failed (exit code {code}): {detail} — "
            "please restart Verba and run setup again"
        )
    _purge_modules_under(target)
    config.ensure_runtime_site_packages()


def _purge_modules_under(target: Path) -> None:
    """Drop stale namespace-package stubs under the target dir from
    sys.modules. A failed import against half-installed debris caches the
    package as a namespace package (no __file__); after the reinstall that
    stub would keep shadowing the real package. Healthy modules stay cached —
    binary extensions cannot be re-imported safely in the same process."""
    prefix = str(target).lower()
    for name, module in list(sys.modules.items()):
        if getattr(module, "__file__", None) is not None:
            continue
        paths = list(getattr(module, "__path__", None) or [])
        if any(str(p).lower().startswith(prefix) for p in paths):
            del sys.modules[name]


def _pip_install_subprocess(packages: list[str], on_line: Callable[[str], None]) -> None:
    if find_spec("pip") is None:
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

    cmd = [sys.executable, "-m", "pip", "install", "--progress-bar", "off", *packages]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8"
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith(("Collecting", "Downloading", "Installing", "Successfully")):
            on_line(line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("pip installation failed")


def install_group(group: FeatureGroup) -> None:
    """pip-install one feature group (frozen build: into <data>/site-packages)."""
    step = group.label
    package_list = ", ".join(group.packages)
    _emit(
        step,
        0,
        f"Bereite Installation von {group.label} vor ({package_list}) ...",
    )
    if config.FROZEN:
        _emit(step, 10, f"Lade {group.label} aus dem Paketbestand ...")
        _pip_install_frozen(group.packages)
    else:
        _emit(step, 10, f"Lade Pakete für {group.label} herunter ...")
        _pip_install_subprocess(
            group.packages,
            lambda line: _emit(step, 50, f"Installationsausgabe: {line}"),
        )
    _emit(step, 90, f"Prüfe {group.label} ...")
    invalidate_caches()
    try:
        import_module(group.import_name)
    except ImportError as exc:
        logger.exception("import of %s failed after installation", group.import_name)
        raise RuntimeError(f"{group.label}: import failed after installation: {exc}") from exc
    _emit(step, 100, f"{group.label} installiert und geprüft.")


def run_setup(include_optional: bool = True) -> None:
    """Run all pending installations sequentially (call from a worker thread)."""
    if not _setup_lock.acquire(blocking=False):
        return
    progress.running = True
    progress.error = ""
    progress.log.clear()
    try:
        if not check_ffmpeg().ok:
            install_ffmpeg()
        for group in FEATURE_GROUPS:
            if not (group.required or include_optional):
                continue
            if not group_installed(group):
                install_group(group)

        settings = config.get_settings()
        settings.setup.completed = True
        config.save_settings(settings)
        _emit("done", 100, "Alle Komponenten installiert und geprüft. Einrichtung abgeschlossen.")
    except Exception as exc:
        logger.exception("setup failed")
        progress.error = str(exc)
        _emit("error", progress.percent, f"Error: {exc}")
    finally:
        progress.running = False
        hub.publish("setup.progress", progress.as_dict())
        _setup_lock.release()
