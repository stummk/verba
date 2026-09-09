"""The CUDA libraries the transcription needs — installed, and made loadable.

The ctranslate2 wheel behind faster-whisper brings its CUDA kernels compiled
in, but not the libraries they link against: cuBLAS 12 and cuDNN 9. Nothing
installs those today, which is why a machine with a perfectly working GPU
transcribes on the CPU — CTranslate2 asks for `libcudnn.so.9` at inference
time, does not get it, and services/whisper.py falls back.

They are not part of a feature group: a gigabyte of CUDA libraries is only
worth downloading on a machine that has an NVIDIA GPU *and* is set to use it.
So this is its own installable component, checked and installed the way ffmpeg
is.

Installing them is only half the job. NVIDIA ships them as pip wheels that
unpack into `site-packages/nvidia/*/lib`, a directory no dynamic loader
searches — `LD_LIBRARY_PATH` would have to name it before the process starts,
which is no help to a running server or a desktop start. `preload()` therefore
loads every library from those directories with `RTLD_GLOBAL` before
CTranslate2 gets to ask: the loader then reuses the object it already holds
under that soname instead of searching for it. On Windows the same problem has
a different answer — the DLLs live in `nvidia/*/bin` and are found once that
directory is on the process' DLL search path.

Whether CUDA works *at all* is a second, independent question, and worth
answering separately: in a container the device nodes are often incomplete
(`/dev/nvidia-uvm` missing is the classic with LXC), and then `nvidia-smi`
still lists the card while every CUDA call fails. No download fixes that, so
`state()` asks the driver first and says so instead of offering a gigabyte.

Only the standard library is used here — setup_check imports this during
bootstrap, and no feature-group module may be imported from there.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

from .. import config
from . import hardware

logger = logging.getLogger(__name__)

#: What pip installs. cuDNN is pinned to 9: that is the major CTranslate2 4.x
#: links against, and a 10 would install without helping anybody.
WHEELS = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12>=9,<10"]

#: German, like every other setup label — it reaches the UI verbatim.
LABEL = "GPU-Beschleunigung (CUDA-Bibliotheken)"

#: Roughly what the two wheels weigh together; named in the progress line
#: because it is the largest download the setup does.
DOWNLOAD_SIZE = "ca. 1 GB"

_WINDOWS = platform.system() == "Windows"

#: Where a wheel keeps its binaries — `lib` on Linux, `bin` on Windows.
_WHEEL_SUBDIR = "bin" if _WINDOWS else "lib"

#: The sonames CTranslate2 dlopens. cuBLAS pulls cuBLASLt in itself, so
#: loading it proves both.
_LINUX_SONAMES = ("libcublas.so.12", "libcudnn.so.9")

#: The same libraries on Windows, where the version is part of the file name
#: — with the name to put in a message, which a glob pattern is not.
_WINDOWS_LIBRARIES = (("cuBLAS", "cublas64_*.dll"), ("cuDNN", "cudnn64_*.dll"))

#: The driver's own library — not shipped by any wheel, it belongs to the
#: installed NVIDIA driver.
_DRIVER_LIBRARIES = ("nvcuda.dll",) if _WINDOWS else ("libcuda.so.1", "libcuda.so")

#: Handles of everything preloaded, kept for the lifetime of the process: a
#: `CDLL` that goes out of scope may be unloaded again, and the DLL directory
#: cookie on Windows must not be closed.
_preloaded: list[Any] = []
_preloaded_dirs: list[Path] = []

_state_cache: dict[str, Any] | None = None
_generation = 0


# ── does this machine want them? ──────────────────────────────────────


def applies() -> bool:
    """Whether these libraries are worth anything here.

    No CUDA on macOS, nothing to accelerate without an NVIDIA GPU, and nothing
    to install when the settings send the transcription to the CPU anyway.
    """
    if platform.system() not in ("Linux", "Windows"):
        return False
    if config.get_settings().whisper.device == "cpu":
        return False
    return hardware.has_gpu()


# ── making the wheels loadable ────────────────────────────────────────


def _site_roots() -> list[Path]:
    """Directories a `nvidia/` package tree could sit in.

    A frozen build installs with `--target` into <data>/site-packages, which
    run.py puts on sys.path — it is named explicitly all the same, so a first
    installation is found without a restart.
    """
    roots: list[Path] = []
    if config.FROZEN:
        roots.append(config.runtime_site_packages())
    roots.extend(Path(entry) for entry in sys.path if entry)
    return roots


def library_dirs() -> list[Path]:
    """Every non-empty `nvidia/*/lib` (or `*/bin`) directory found on sys.path."""
    found: list[Path] = []
    for root in _site_roots():
        base = root / "nvidia"
        try:
            packages = sorted(base.iterdir()) if base.is_dir() else []
        except OSError:
            continue
        for package in packages:
            candidate = package / _WHEEL_SUBDIR
            try:
                if candidate not in found and candidate.is_dir() and any(candidate.iterdir()):
                    found.append(candidate)
            except OSError:
                continue
    return found


def _load_all(directory: Path) -> None:
    """Load every shared library in `directory` into the global namespace.

    cuDNN 9 is split into sub-libraries that need each other, and glob order
    is not dependency order — so whatever fails is tried once more after the
    rest is in. What still fails afterwards was not ours to load: a stub, or a
    library for another CUDA major.
    """
    pending = sorted(directory.glob("lib*.so*"))
    while pending:
        failed: list[Path] = []
        for path in pending:
            try:
                _preloaded.append(ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL))
            except OSError as exc:
                logger.debug("preload of %s failed: %s", path.name, exc)
                failed.append(path)
        if len(failed) == len(pending):
            break  # a second pass would fail the same way
        pending = failed


def preload() -> list[Path]:
    """Make the pip-installed CUDA libraries findable; returns what was added.

    Idempotent and cheap after the first call — every directory is handled
    once, and a machine without the wheels has nothing to do here.
    """
    fresh = [directory for directory in library_dirs() if directory not in _preloaded_dirs]
    for directory in fresh:
        _preloaded_dirs.append(directory)
        if _WINDOWS:
            try:
                _preloaded.append(os.add_dll_directory(str(directory)))
            except OSError as exc:
                logger.debug("cannot add %s to the DLL search path: %s", directory, exc)
            continue
        _load_all(directory)
    if fresh:
        logger.info("CUDA libraries preloaded from %s", ", ".join(str(p) for p in fresh))
    return fresh


# ── is CUDA actually usable? ──────────────────────────────────────────


def driver_state() -> tuple[bool, str]:
    """Whether the CUDA driver answers; the detail is for the log.

    `cuInit` is the call an incomplete container breaks: without
    `/dev/nvidia-uvm` it fails while `nvidia-smi` — which does not need that
    device — keeps listing the card.
    """
    for name in _DRIVER_LIBRARIES:
        try:
            driver = ctypes.CDLL(name)
        except OSError:
            continue
        try:
            code = int(driver.cuInit(0))
        except (AttributeError, OSError) as exc:
            return False, f"{name}: cuInit unavailable ({exc})"
        if code == 0:
            return True, name
        return False, f"cuInit → {code}"
    return False, f"not found: {', '.join(_DRIVER_LIBRARIES)}"


def _libraries_present() -> tuple[bool, str]:
    """Whether cuBLAS and cuDNN can be found; the detail names what cannot.

    On Linux the loader is asked directly — that also covers libraries a
    distribution package installed, which no file check would find. On
    Windows the files are only looked for: loading a DLL would lock it for the
    lifetime of the process, and pip could then not replace it any more.
    """
    if _WINDOWS:
        directories = [*library_dirs()]
        directories.extend(
            Path(entry) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry
        )
        missing = []
        for label, pattern in _WINDOWS_LIBRARIES:
            if not any(_has_match(directory, pattern) for directory in directories):
                missing.append(label)
        return not missing, ", ".join(missing)
    missing = []
    for soname in _LINUX_SONAMES:
        try:
            _preloaded.append(ctypes.CDLL(soname, mode=ctypes.RTLD_GLOBAL))
        except OSError:
            missing.append(soname)
    return not missing, ", ".join(missing)


def _has_match(directory: Path, pattern: str) -> bool:
    try:
        return directory.is_dir() and any(directory.glob(pattern))
    except OSError:
        return False


def _measure() -> dict[str, Any]:
    """The actual verdict, with a German sentence for the UI."""
    if not applies():
        return {
            "applies": False,
            "ok": False,
            "installable": False,
            "libraries": False,
            "driver": False,
            "detail": "",
        }
    preload()
    libraries, missing = _libraries_present()
    driver, driver_detail = driver_state()
    if libraries and driver:
        detail = "Einsatzbereit — die Transkription kann die GPU nutzen."
    elif not driver:
        logger.warning("CUDA driver not usable: %s", driver_detail)
        detail = (
            "Die GPU wird erkannt, aber der CUDA-Treiber antwortet nicht "
            f"({driver_detail}). In einem Container fehlen dafür meist die Geräte "
            "/dev/nvidia-uvm und /dev/nvidia-uvm-tools, oder die Treiberversion "
            "im Container passt nicht zu der des Hosts."
        )
    else:
        detail = (
            f"Nicht installiert ({missing}) — die Transkription läuft bis dahin "
            f"auf der CPU. Download {DOWNLOAD_SIZE}."
        )
    return {
        "applies": True,
        "ok": libraries and driver,
        # only worth offering while the driver works: the libraries alone
        # cannot fix a GPU that does not answer
        "installable": driver and not libraries,
        "libraries": libraries,
        "driver": driver,
        "detail": detail,
    }


def state(*, refresh: bool = False) -> dict[str, Any]:
    """Whether the GPU can be used for the transcription, and what is missing.

    Cached: the status endpoint is polled, and asking the driver plus loading
    two large libraries is not something to repeat per request. The cache is
    dropped when an installation changed the answer.
    """
    global _state_cache
    if _state_cache is None or refresh:
        _state_cache = _measure()
    return dict(_state_cache)


def invalidate() -> None:
    """Drop the cached verdict (after a settings change, and in tests)."""
    global _state_cache
    _state_cache = None


def generation() -> int:
    """Counts installations of these libraries.

    services/whisper.py remembers a failed CUDA attempt for the lifetime of
    the process. That memory is only valid as long as the libraries are the
    same ones — this is how it learns that they are not.
    """
    return _generation


def mark_installed() -> None:
    """Called after pip put the libraries in place."""
    global _generation
    _generation += 1
    invalidate()
    preload()
