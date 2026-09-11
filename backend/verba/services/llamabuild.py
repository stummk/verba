"""Compiling llama.cpp with CUDA on this machine.

llama.cpp publishes CUDA binaries for Windows only. On Linux the ladder in
services/llamacpp.py first tries the Vulkan build — that reaches an NVIDIA
card through the driver's ICD and costs 30 MB instead of a toolkit. But a
container often has no ICD, and then the only remaining way to the GPU is a
build of our own: the source archive of the same release, configured for the
card in this machine, compiled here.

That is a heavy step and it is treated as one. It runs last, only where a
prebuilt package failed to reach the GPU, and it says what it does at every
turn — the build tools and the CUDA toolkit are installed through the same
package machinery the loader repair uses, which means as root and from a
fixed table of package names. When anything about it fails, the installation
does not: services/llamacpp.py falls back to the CPU build, and llama.cpp
stays usable.

Two decisions keep the build within reason on a server:

- `CMAKE_CUDA_ARCHITECTURES=native` compiles for the installed card only.
  Building for every architecture llama.cpp supports takes several times as
  long and produces a binary this machine will never need.
- The number of parallel compilers is capped by RAM, not by cores. Each nvcc
  of ggml-cuda takes the better part of a gigabyte, so `-j$(nproc)` on a
  16-core container with 8 GB is how a build dies without an error anyone can
  read.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .. import config, procutil
from . import cudalibs, download, hardware

logger = logging.getLogger(__name__)

#: The source archive of one release — the same tag the binaries come from,
#: so a self-built server is the same version as the packaged one.
SOURCE_URL = "https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.tar.gz"
#: The source tree is ~30 MB packed; anything far beyond that is not it.
MAX_SOURCE_BYTES = 200 * 1024 * 1024
#: Objects, libraries and the CUDA fat binaries of one build.
BUILD_SPACE_BYTES = 8 * 1024 * 1024 * 1024
#: A CUDA build takes minutes, not seconds — and on a small machine many of
#: them. This is the point at which something is wrong rather than slow.
BUILD_TIMEOUT_S = 90 * 60
#: Downloading a CUDA toolkit is measured in gigabytes.
PACKAGE_TIMEOUT_S = 60 * 60
#: Memory one nvcc of ggml-cuda needs; the cap on parallel compilers.
_MB_PER_COMPILER = 2048
#: How much build output is kept in memory — only the tail reaches a message.
_KEPT_LINES = 50

#: The tools a CUDA build needs, and the packages that carry them — the same
#: shape as `llamacpp._LINUX_LIBRARIES`, and just as closed: nothing else is
#: ever handed to a package manager.
TOOLS: dict[str, dict[str, tuple[str, ...]]] = {
    "cmake": {
        "apt": ("cmake",),
        "dnf": ("cmake",),
        "zypper": ("cmake",),
        "pacman": ("cmake",),
        "apk": ("cmake",),
    },
    "make": {
        "apt": ("make", "build-essential"),
        "dnf": ("make",),
        "zypper": ("make",),
        "pacman": ("make",),
        "apk": ("make",),
    },
    "g++": {
        "apt": ("g++", "build-essential"),
        "dnf": ("gcc-c++",),
        "zypper": ("gcc-c++",),
        "pacman": ("gcc",),
        "apk": ("g++",),
    },
    "nvcc": {
        "apt": ("nvidia-cuda-toolkit",),
        "dnf": ("cuda-nvcc", "cuda-toolkit"),
        "zypper": ("cuda-nvcc",),
        "pacman": ("cuda",),
        "apk": (),
    },
}

#: Where a CUDA toolkit puts nvcc when it is not on the PATH.
_NVCC_PATHS = (
    "/usr/local/cuda/bin/nvcc",
    "/usr/lib/nvidia-cuda-toolkit/bin/nvcc",
    "/opt/cuda/bin/nvcc",
)

#: cmake's progress, e.g. "[ 42%] Building CUDA object ...".
_PROGRESS = re.compile(r"^\[\s*(\d+)%\]")
#: A cmake too old for CUDA_ARCHITECTURES=native says so about this variable.
_ARCHITECTURES_REJECTED = "cuda_architectures"


def _find(tool: str) -> str | None:
    """The tool's path — nvcc also where a toolkit puts it without a PATH entry."""
    found = shutil.which(tool)
    if found:
        return found
    if tool == "nvcc":
        return next((path for path in _NVCC_PATHS if Path(path).is_file()), None)
    return None


def missing_tools() -> list[str]:
    """Which of TOOLS this machine has not got."""
    return [tool for tool in TOOLS if _find(tool) is None]


def possible() -> tuple[bool, str]:
    """Whether a CUDA build can be attempted here, and why not when it cannot.

    The reason is German and reaches the installation log — but only where it
    is worth saying. A machine without an NVIDIA card, or one whose platform
    has official CUDA binaries anyway, gets no reason at all: nothing is
    missing there.
    """
    if platform.system() != "Linux":
        return False, ""  # Windows has CUDA packages, macOS has no CUDA
    if not hardware.has_gpu():
        return False, ""
    driver_ok, driver_detail = cudalibs.driver_state()
    if not driver_ok:
        return False, (
            f"Der CUDA-Treiber antwortet nicht ({driver_detail}) — ein selbst gebauter "
            "CUDA-Build würde die Karte genauso wenig erreichen. In einem Container "
            "fehlen dafür meist /dev/nvidia-uvm und /dev/nvidia-uvm-tools."
        )
    missing = missing_tools()
    if missing:
        from . import llamacpp

        if not llamacpp.can_install_packages():
            return False, (
                f"Zum Bauen fehlen {', '.join(missing)}, und Systempakete lassen sich "
                "hier nicht installieren (kein root, kein passwortloses sudo)."
            )
    return True, ""


def build(tag: str, emit: Any, *, base: int = 0, span: int = 100) -> Path:
    """Compile llama-server with CUDA and install it; returns its path.

    Raises when anything goes wrong — the caller then keeps walking its ladder
    down to the CPU build, so a failure here costs speed, never the feature.
    """
    from . import llamacpp

    def step(fraction: float, message: str) -> None:
        emit(base + int(span * fraction), message)

    step(0.02, "Prüfe die Werkzeuge für den CUDA-Build ...")
    _ensure_tools(emit, base, span)

    workspace = config.tools_dir() / "llama-build"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        download.require_free_space(workspace, BUILD_SPACE_BYTES)
        source = _fetch_source(tag, workspace, emit, base, span)
        build_dir = workspace / "build"
        _configure(source, build_dir, emit, base, span)
        _compile(build_dir, emit, base, span)
        step(0.97, "Installiere den gebauten llama-server ...")
        return _install(build_dir, llamacpp.binary_dir() / "cuda-source")
    finally:
        # several gigabytes of objects; the binary is out by now
        shutil.rmtree(workspace, ignore_errors=True)


def _ensure_tools(emit: Any, base: int, span: int) -> None:
    """Install what is missing, and refuse the build when it stays missing."""
    from . import llamacpp

    missing = missing_tools()
    if not missing:
        return
    emit(base + int(span * 0.05), f"Installiere Build-Werkzeuge: {', '.join(missing)} ...")
    if "nvcc" in missing:
        emit(
            base + int(span * 0.05),
            "Das CUDA-Toolkit ist mehrere GB groß — das dauert einige Minuten.",
        )
    llamacpp.install_packages(
        TOOLS,
        missing,
        emit,
        percent=base + int(span * 0.1),
        timeout=PACKAGE_TIMEOUT_S,
    )
    still_missing = missing_tools()
    if still_missing:
        raise RuntimeError(
            f"Zum Bauen fehlen weiterhin {', '.join(still_missing)} — "
            "ohne Compiler und CUDA-Toolkit ist kein CUDA-Build möglich"
        )


def _fetch_source(tag: str, workspace: Path, emit: Any, base: int, span: int) -> Path:
    """Download and unpack the source archive of that release."""
    from . import llamacpp

    archive = workspace / f"llama.cpp-{tag}.tar.gz"
    url = SOURCE_URL.format(tag=tag)
    emit(base + int(span * 0.12), f"Lade den Quellcode von llama.cpp {tag} ...")
    download.fetch(
        url,
        archive,
        MAX_SOURCE_BYTES,
        download.phase(emit, base + int(span * 0.12), int(span * 0.08), "Lade den Quellcode ..."),
    )
    emit(base + int(span * 0.2), "Entpacke den Quellcode ...")
    llamacpp._extract_archive(archive, workspace)
    archive.unlink(missing_ok=True)
    root = next(
        (
            path
            for path in workspace.iterdir()
            if path.is_dir() and (path / "CMakeLists.txt").is_file()
        ),
        None,
    )
    if root is None:
        raise RuntimeError("Im Quellarchiv von llama.cpp war kein CMake-Projekt zu finden")
    return root


def _cmake_arguments(source: Path, build_dir: Path, *, native: bool) -> list[str]:
    arguments = [
        _find("cmake") or "cmake",
        "-S",
        str(source),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DGGML_CUDA=ON",
        # one binary to copy, nothing to find next to it at run time
        "-DBUILD_SHARED_LIBS=OFF",
        "-DLLAMA_BUILD_SERVER=ON",
        "-DLLAMA_BUILD_TOOLS=ON",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_EXAMPLES=OFF",
        # the server fetches nothing itself; Verba downloads the models
        "-DLLAMA_CURL=OFF",
    ]
    if native:
        arguments.append("-DCMAKE_CUDA_ARCHITECTURES=native")
    nvcc = _find("nvcc")
    if nvcc and not shutil.which("nvcc"):
        # a toolkit that is installed but not on the PATH
        arguments.append(f"-DCMAKE_CUDA_COMPILER={nvcc}")
    return arguments


def _configure(source: Path, build_dir: Path, emit: Any, base: int, span: int) -> None:
    """Run cmake, once for this card and once for whatever cmake accepts."""
    percent = base + int(span * 0.25)
    emit(base + int(span * 0.22), "Konfiguriere den Build (cmake) ...")
    lines: deque[str] = deque(maxlen=_KEPT_LINES)
    for native in (True, False):
        code = _run_streaming(
            _cmake_arguments(source, build_dir, native=native),
            source,
            lambda line: _log_line(line, lines, emit, percent),
        )
        if code == 0:
            return
        # cmake older than 3.24 does not know "native" — building for the
        # architectures llama.cpp defaults to is slower but still CUDA
        if not native or _ARCHITECTURES_REJECTED not in " ".join(lines).lower():
            break
        emit(percent, "cmake kennt diese Karte nicht — konfiguriere ohne CUDA_ARCHITECTURES ...")
        shutil.rmtree(build_dir, ignore_errors=True)
        lines.clear()
    raise RuntimeError(f"cmake ist fehlgeschlagen: {_tail(lines)}")


def _compile(build_dir: Path, emit: Any, base: int, span: int) -> None:
    """Build the llama-server target, reporting cmake's own percentage."""
    jobs = _parallel_jobs()
    emit(
        base + int(span * 0.3),
        f"Kompiliere llama-server mit CUDA ({jobs} parallel) — das dauert einige Minuten ...",
    )
    # a CUDA build compiles several hundred files, and every emit broadcasts
    # the whole installation log to every client — so only a step of the bar
    # is worth an event, and only the tail of the output is worth keeping
    lines: deque[str] = deque(maxlen=_KEPT_LINES)
    reported = -1

    def on_line(line: str) -> None:
        nonlocal reported
        match = _PROGRESS.match(line)
        if match:
            # cmake's 0..100 becomes the 30..95 the compilation owns here
            percent = base + int(span * (0.3 + 0.65 * min(int(match.group(1)), 100) / 100))
            lines.append(line)
            if percent != reported:
                reported = percent
                emit(percent, line)
            return
        _log_line(line, lines, emit, base + int(span * 0.3))

    code = _run_streaming(
        [
            _find("cmake") or "cmake",
            "--build",
            str(build_dir),
            "--config",
            "Release",
            "--target",
            "llama-server",
            "-j",
            str(jobs),
        ],
        build_dir,
        on_line,
    )
    if code != 0:
        raise RuntimeError(f"Der Compiler ist fehlgeschlagen: {_tail(lines)}")


def _parallel_jobs() -> int:
    """How many compilers this machine can afford to run at once.

    Bounded by memory rather than by cores: every nvcc translating ggml-cuda
    wants close to a gigabyte, and a container with many cores and little RAM
    is where `-j$(nproc)` turns into an OOM kill with no readable error.
    """
    cores = os.cpu_count() or 1
    ram_total_mb, _ = hardware.ram_mb()
    if ram_total_mb <= 0:
        return max(1, min(cores, 2))
    return max(1, min(cores, ram_total_mb // _MB_PER_COMPILER))


def _install(build_dir: Path, dest: Path) -> Path:
    """Copy the built binaries into their own candidate directory."""
    from . import llamacpp

    produced = build_dir / "bin"
    binary = llamacpp._server_binary_in(produced) if produced.is_dir() else None
    if binary is None:
        raise RuntimeError("Der Build lief durch, hat aber keinen llama-server erzeugt")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(binary.parent, dest, dirs_exist_ok=True)
    installed = llamacpp._server_binary_in(dest)
    if installed is None:  # pragma: no cover — copytree just put it there
        raise RuntimeError("Der gebaute llama-server ließ sich nicht installieren")
    installed.chmod(0o755)
    logger.info("built llama-server installed: %s", installed)
    return installed


def _log_line(line: str, lines: deque[str], emit: Any, percent: int) -> None:
    """Keep every line for the error message, show the ones worth showing."""
    lines.append(line)
    lowered = line.lower()
    # cmake's status lines, plus anything that mentions an error at all
    if lowered.startswith("--") or "error" in lowered:
        emit(percent, line)


def _tail(lines: deque[str], count: int = 3) -> str:
    kept = list(lines)[-count:]
    return " | ".join(line for line in kept if line.strip()) or "keine Ausgabe"


def _run_streaming(cmd: list[str], cwd: Path, on_line: Any) -> int:
    """Run a build command and hand every output line on as it appears.

    A build has no progress other than what it prints, and it runs long enough
    that a silent wait would look like a hang. The watchdog is a timer rather
    than a deadline per line: a compiler that wedges prints nothing at all.
    """
    logger.info("build step: %s", " ".join(cmd))
    process = procutil.popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        bufsize=1,
        # a compiler that outlives Verba keeps every core it was given
        kill_with_parent=True,
    )
    timer = threading.Timer(BUILD_TIMEOUT_S, process.kill)
    timer.daemon = True
    timer.start()
    started = time.monotonic()
    try:
        for raw in process.stdout or ():
            line = raw.rstrip()
            if line:
                on_line(line)
        code = process.wait()
    finally:
        timer.cancel()
    logger.info("build step finished with %s after %.0fs", code, time.monotonic() - started)
    return code
