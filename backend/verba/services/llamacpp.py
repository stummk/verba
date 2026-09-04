"""Local LLM support via llama.cpp (`llama-server`).

One-click local setup, mirroring the ffmpeg approach:
- the llama.cpp release binary is downloaded into <data>/tools/llama/
- GGUF models are downloaded into the configured models directory
  (default <data>/models/llm) — any .gguf already lying there is used from
  there, so an existing collection needs no second download
- a hardware probe (RAM/VRAM) recommends a model + quantisation and rates
  every catalog entry ("runs / tight / too large", see services/hardware.py);
  the choice itself stays with the user
- `llama-server` runs as a managed subprocess speaking the OpenAI protocol,
  so the normal LLM client (services/llm.py) needs no special casing.

Progress is reported via "model.download" and "engine.status" events.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from .. import config, procutil
from ..events import hub
from . import hardware

logger = logging.getLogger(__name__)

RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
RELEASE_TAG_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{tag}"
RELEASE_LIST_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"
#: Only asset of the semver release that "latest" answers with — it names the
#: nightly build that carries the binaries (see resolve_release).
NIGHTLY_POINTER = "nightly-tag.txt"
#: The Windows CUDA runtime archive is the largest single download.
MAX_BINARY_BYTES = 900 * 1024 * 1024
MAX_MODEL_BYTES = 12 * 1024 * 1024 * 1024
#: A multi-gigabyte download over a shaky line gets more than one chance.
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_RETRY_DELAY_S = 3
SERVER_PORT = 8711
SERVER_STARTUP_TIMEOUT_S = 180  # first start loads the model from disk

# Curated catalog: multilingual instruct models with large context, sized for
# common hardware tiers, ordered by the hardware they need. `recommended`
# marks the line the hardware probe suggests — the others are equally usable
# alternatives, and any .gguf placed in the models directory works too.
#
# Qwen3 comes from the official Qwen GGUF repositories. Google's own Gemma
# GGUF repositories are gated (they answer 401 without an accepted licence
# and a token), so the Gemma builds come from the unsloth mirrors.
MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "Qwen3-1.7B-Q8_0",
        "file": "Qwen3-1.7B-Q8_0.gguf",
        "url": "https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q8_0.gguf",
        "size_mb": 2100,
        "min_free_mb": 3500,
        "recommended": True,
        "label": "Qwen3 1.7B (klein — ab 4 GB RAM/VRAM)",
    },
    {
        "name": "Qwen3-4B-Q4_K_M",
        "file": "Qwen3-4B-Q4_K_M.gguf",
        "url": "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf",
        "size_mb": 2600,
        "min_free_mb": 5000,
        "recommended": True,
        "label": "Qwen3 4B (ab 6 GB RAM/VRAM)",
    },
    {
        "name": "gemma-3-4b-it-Q4_K_M",
        "file": "gemma-3-4b-it-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/unsloth/gemma-3-4b-it-GGUF/resolve/main/"
            "gemma-3-4b-it-Q4_K_M.gguf"
        ),
        "size_mb": 2400,
        "min_free_mb": 5500,
        "label": "Gemma 3 4B (Alternative zu Qwen3 4B)",
    },
    {
        "name": "Qwen3-8B-Q4_K_M",
        "file": "Qwen3-8B-Q4_K_M.gguf",
        "url": "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
        "size_mb": 5200,
        "min_free_mb": 9000,
        "recommended": True,
        "label": "Qwen3 8B (ab 10 GB VRAM oder 20 GB RAM)",
    },
    {
        "name": "gemma-3-12b-it-Q4_K_M",
        "file": "gemma-3-12b-it-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/unsloth/gemma-3-12b-it-GGUF/resolve/main/"
            "gemma-3-12b-it-Q4_K_M.gguf"
        ),
        "size_mb": 7000,
        "min_free_mb": 13000,
        "label": "Gemma 3 12B (beste Qualität — ab 14 GB VRAM oder 28 GB RAM)",
    },
]

_download_lock = threading.Lock()
_downloads_running: set[str] = set()

#: The last (or running) binary installation, so the UI can show what is
#: happening — and still show it after a reload or a switch of view. The
#: wizard installs llama.cpp in its LLM step and displays this log live.
_INSTALL_LOG_LINES = 200
_install_state: dict[str, Any] = {
    "running": False,
    "percent": 0,
    "detail": "",
    "error": "",
    "log": [],
}


# ── paths ─────────────────────────────────────────────────────────────


def binary_dir() -> Path:
    return config.tools_dir() / "llama"


def llm_models_dir() -> Path:
    """Where the GGUF files live — configurable, see config.llm_models_dir."""
    return config.llm_models_dir()


def server_binary() -> Path | None:
    """The installed llama-server, newest first.

    Every release unpacks into its own build directory, so after an update two
    of them lie side by side — the freshest one is the installation that was
    verified, and the one to run.
    """
    exe = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    candidates = [path for path in binary_dir().rglob(exe) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


# ── hardware probe & recommendation ───────────────────────────────────


def probe_hardware() -> dict[str, Any]:
    """RAM/VRAM of this machine — see services/hardware.py."""
    return hardware.probe()


def model_needs_mb(entry: dict[str, Any]) -> int:
    """Memory this catalog entry needs while serving (weights + context)."""
    return int(entry.get("min_free_mb") or hardware.gguf_requirement(entry.get("size_mb", 0)))


def recommend_model(hw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Largest fitting catalog model for the available VRAM (GPU) or half the RAM.

    Only `recommended` entries are suggested, so adding an alternative of the
    same size to the catalog does not silently change what new users get. This
    looks at the *total* memory (what the machine can do); whether it fits
    right now is `fit_for()`.
    """
    hw = hw or probe_hardware()
    budget_mb = hw["vram_total_mb"] if hw["vram_total_mb"] > 0 else hw["ram_total_mb"] // 2
    fitting = [m for m in MODEL_CATALOG if m["min_free_mb"] <= budget_mb]
    preferred = [m for m in fitting if m.get("recommended")]
    return (preferred or fitting or [MODEL_CATALOG[0]])[-1]


def fit_for(entry: dict[str, Any], hw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verdict for one catalog entry: does this system run it, and where?"""
    return hardware.check_llm_model(model_needs_mb(entry), hw=hw)


# ── binary installation ───────────────────────────────────────────────


ARCHIVE_SUFFIXES = (".zip", ".tar.gz")

#: Exit code Windows gives a process whose DLLs could not be resolved.
_WIN_DLL_NOT_FOUND = 0xC0000135

#: What a dynamic linker prints when a library the binary needs is missing.
_LOADER_ERRORS = (
    "error while loading shared libraries",
    "cannot open shared object",
    "symbol lookup error",
    "glibc_",
    "glibcxx_",
)

#: Libraries the official Linux build loads from the system — everything else
#: it needs ships in the archive — and the packages that carry them. Several
#: candidates per manager because distributions rename: Debian 13 and
#: Ubuntu 24.04 call OpenSSL 3 "libssl3t64", their predecessors "libssl3".
#: Only names from this table are ever handed to a package manager.
_LINUX_LIBRARIES: dict[str, dict[str, tuple[str, ...]]] = {
    "libstdc++.so.6": {
        "apt": ("libstdc++6",),
        "dnf": ("libstdc++",),
        "zypper": ("libstdc++6",),
        "pacman": ("gcc-libs",),
        "apk": ("libstdc++",),
    },
    "libgcc_s.so.1": {
        "apt": ("libgcc-s1", "libgcc1"),
        "dnf": ("libgcc",),
        "zypper": ("libgcc_s1",),
        "pacman": ("gcc-libs",),
        "apk": ("libgcc",),
    },
    # ggml is built with OpenMP; a minimal server installation has no libgomp
    "libgomp.so.1": {
        "apt": ("libgomp1",),
        "dnf": ("libgomp",),
        "zypper": ("libgomp1",),
        "pacman": ("gcc-libs",),
        "apk": ("libgomp",),
    },
    "libssl.so.3": {
        "apt": ("libssl3", "libssl3t64"),
        "dnf": ("openssl-libs",),
        "zypper": ("libopenssl3",),
        "pacman": ("openssl",),
        "apk": ("openssl",),
    },
    "libcrypto.so.3": {
        "apt": ("libssl3", "libssl3t64"),
        "dnf": ("openssl-libs",),
        "zypper": ("libopenssl3",),
        "pacman": ("openssl",),
        "apk": ("openssl",),
    },
}

#: Package managers in the order they are tried: key into _LINUX_LIBRARIES,
#: the program, the arguments that install without asking anything, and how to
#: refresh the package lists when an install fails for the lack of them.
_PACKAGE_MANAGERS: tuple[tuple[str, str, list[str], list[str]], ...] = (
    ("apt", "apt-get", ["install", "-y", "--no-install-recommends"], ["update"]),
    ("dnf", "dnf", ["install", "-y"], []),
    ("dnf", "yum", ["install", "-y"], []),
    ("zypper", "zypper", ["--non-interactive", "install"], ["--non-interactive", "refresh"]),
    ("pacman", "pacman", ["-S", "--noconfirm"], ["-Sy", "--noconfirm"]),
    ("apk", "apk", ["add", "--no-cache"], ["update"]),
)

#: A library name as the dynamic linker prints it.
_MISSING_LIBRARY = re.compile(r"lib[\w.+-]*?\.so(?:\.\d+)*")

_PACKAGE_TIMEOUT_S = 300


class _LoaderFailure(RuntimeError):
    """The binary is installed but the system cannot load it.

    `missing` names the libraries that are absent and installable — empty
    when the system has them in a version that is too old, which no package
    manager can fix.
    """

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


#: What the two architectures llama.cpp publishes builds for call themselves.
_ARM_MACHINES = {"arm64", "aarch64", "armv8l", "armv8b"}
_X64_MACHINES = {"x86_64", "amd64", "x64", "em64t"}


def _is_arm() -> bool:
    return _arch_token() == "arm64"


def _arch_token() -> str:
    """The architecture every asset name carries — "arm64", "x64", or nothing.

    An architecture nobody publishes a build for (s390x, 32-bit x86) answers
    with an empty string, which leaves no asset to install: refusing beats
    unpacking a binary this machine cannot execute.
    """
    machine = platform.machine().lower()
    if machine in _ARM_MACHINES:
        return "arm64"
    if machine in _X64_MACHINES:
        return "x64"
    return ""


def _asset_patterns() -> list[str]:
    """Asset name fragments for this platform, best first.

    llama.cpp publishes no CUDA build for Linux, and its Vulkan build needs a
    loader that a server installation usually has not got — so Linux gets the
    plain CPU build, which runs everywhere. On Windows the CUDA 12 build comes
    first because it still works with older drivers than the CUDA 13 one.
    """
    system = platform.system()
    if not _arch_token():
        return []
    if system == "Windows":
        if _is_arm():
            return ["bin-win-cpu-arm64"]
        if hardware.has_gpu():
            return ["bin-win-cuda-12", "bin-win-cuda", "bin-win-cpu-x64"]
        return ["bin-win-cpu-x64"]
    if system == "Linux":
        return ["bin-ubuntu-arm64"] if _is_arm() else ["bin-ubuntu-x64"]
    if system == "Darwin":
        return ["bin-macos-arm64"] if _is_arm() else ["bin-macos-x64"]
    return []


def _pick_release_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The archive to install from one release.

    Windows ships .zip, Linux and macOS .tar.gz — both are accepted, so a
    change of packaging on one platform does not break the other. The
    architecture is required on top of the pattern: a release that drops one
    CUDA version would otherwise let the broader "bin-win-cuda" fragment match
    the arm64 build on an x64 machine.
    """
    arch = _arch_token()
    for pattern in _asset_patterns():
        for asset in assets:
            name = asset.get("name", "")
            if name.startswith("cudart-"):
                continue  # the CUDA runtime, fetched alongside the build it belongs to
            if pattern in name and arch in name and name.endswith(ARCHIVE_SUFFIXES):
                return asset
    return None


def _pick_cudart_asset(assets: list[dict[str, Any]], binary_name: str) -> dict[str, Any] | None:
    """The CUDA runtime archive belonging to a Windows CUDA build, if one is needed.

    `llama-<build>-bin-win-cuda-<ver>-<arch>.zip` contains ggml-cuda.dll but
    none of the CUDA runtime DLLs it links against. They ship separately as
    `cudart-llama-bin-win-cuda-<ver>-<arch>.zip`, and without them ggml
    silently fails to load its CUDA backend — the GPU would stay unused.
    """
    if "-cuda" not in binary_name:
        return None
    variant = binary_name.split("-bin-", 1)[-1]  # e.g. "win-cuda-12.4-x64.zip"
    return next((a for a in assets if a.get("name") == f"cudart-llama-bin-{variant}"), None)


def _get_json(url: str) -> Any:
    response = httpx.get(
        url,
        timeout=30,
        follow_redirects=True,
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    return response.json()


def _fetch_text(url: str) -> str:
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.text


def resolve_release() -> tuple[dict[str, Any], dict[str, Any]]:
    """Newest release carrying a binary for this platform, and that asset.

    The release layout changed: `releases/latest` now answers with a semver
    release whose only asset is `nightly-tag.txt`, naming the nightly build
    that has the binaries. That pointer is followed, a release carrying the
    assets itself is still used directly, and if neither does, the release
    list is scanned from the top.
    """
    latest = _get_json(RELEASE_API)
    candidates = [latest]
    pointer = next((a for a in latest.get("assets", []) if a.get("name") == NIGHTLY_POINTER), None)
    if pointer is not None:
        tag = _fetch_text(pointer["browser_download_url"]).strip()
        if tag:
            candidates.append(_get_json(RELEASE_TAG_API.format(tag=tag)))
    for candidate in candidates:
        asset = _pick_release_asset(candidate.get("assets", []))
        if asset is not None:
            return candidate, asset

    for candidate in _get_json(RELEASE_LIST_API) or []:
        if not isinstance(candidate, dict):
            continue
        asset = _pick_release_asset(candidate.get("assets", []))
        if asset is not None:
            return candidate, asset
    logger.error(
        "no llama.cpp asset for %s/%s in the current releases",
        platform.system(),
        platform.machine(),
    )
    raise RuntimeError(
        f"Für dieses System ({platform.system()} {platform.machine()}) "
        "gibt es kein llama.cpp-Release"
    )


def install_state() -> dict[str, Any]:
    """Snapshot of the running (or last) llama.cpp installation, log included."""
    return {**_install_state, "log": list(_install_state["log"])}


def _install_event(percent: int, message: str, state: str = "running") -> None:
    """Record one installation step and broadcast it with the log so far.

    The percent ticks of a download repeat the same message; only new lines
    reach the log, so it reads as a protocol and not as a progress bar.
    """
    log = _install_state["log"]
    if message and (not log or log[-1] != message):
        log.append(message)
        del log[:-_INSTALL_LOG_LINES]
    _install_state["percent"] = percent
    _install_state["running"] = state == "running"
    if message:
        _install_state["detail"] = message
    if state == "error":
        _install_state["error"] = message
    hub.publish(
        "model.download",
        {
            "scope": "llm-binary",
            "name": "llama.cpp",
            "state": state,
            "percent": percent,
            "detail": message,
            "log": list(log),
        },
    )


def start_binary_install() -> bool:
    """Install the llama.cpp binary in a background thread (API entry point)."""
    with _download_lock:
        if "llama.cpp" in _downloads_running:
            return False
        _downloads_running.add("llama.cpp")
    _install_state.update(running=True, percent=0, detail="", error="", log=[])

    def run() -> None:
        try:
            install_binary()
        except Exception as exc:
            logger.exception("llama.cpp installation failed")
            _install_event(0, _download_error(exc), state="error")
        finally:
            _install_state["running"] = False
            with _download_lock:
                _downloads_running.discard("llama.cpp")

    threading.Thread(target=run, daemon=True, name="llama-setup").start()
    return True


def install_binary(report: Any = None) -> str:
    """Download the current llama.cpp release binary; returns the server path."""

    def emit(percent: int, message: str, state: str = "running") -> None:
        _install_event(percent, message, state)
        if report:
            report(percent, message)

    existing = server_binary()
    if existing is not None:
        emit(100, f"llama.cpp ist bereits installiert: {existing}", state="done")
        return str(existing)

    emit(0, f"System: {platform.system()} {platform.machine()}")
    emit(0, "Suche aktuelles llama.cpp-Release ...")
    release, asset = resolve_release()
    logger.info("installing llama.cpp %s: %s", release.get("tag_name", "?"), asset["name"])
    emit(2, f"Release {release.get('tag_name', '?')}, Paket {asset['name']}")

    downloads = [(asset, "llama.cpp")]
    cudart = _pick_cudart_asset(release.get("assets", []), asset["name"])
    if cudart is not None:
        downloads.append((cudart, "CUDA-Laufzeit"))

    dest = binary_dir()
    dest.mkdir(parents=True, exist_ok=True)
    # archive plus unpacked content, and only the compressed size is known
    _require_free_space(dest, sum(int(item.get("size", 0)) for item, _ in downloads) * 3)

    span = 96 // len(downloads)
    for index, (item, label) in enumerate(downloads):
        size = int(item.get("size", 0))
        if size > MAX_BINARY_BYTES:
            raise RuntimeError(f"{label}-Download zu groß ({size} Bytes)")
        base = index * span
        archive = dest / item["name"]
        # a leftover from an earlier run cannot be verified — only resume within this one
        archive.unlink(missing_ok=True)
        emit(base, f"Lade {label} ({size // (1024 * 1024)} MB) ...")
        _download_file(
            item["browser_download_url"],
            archive,
            MAX_BINARY_BYTES,
            _phase(emit, base, span, f"Lade {label} ..."),
        )
        emit(base + span, f"Entpacke {label} ...")
        _extract_archive(archive, dest)
        archive.unlink(missing_ok=True)

    binary = server_binary()
    if binary is None:
        logger.error("no llama-server in %s", asset["name"])
        raise RuntimeError("Im llama.cpp-Archiv war kein llama-server enthalten")
    if platform.system() != "Windows":
        binary.chmod(0o755)
    emit(97, "Prüfe, ob llama-server auf diesem System startet ...")
    try:
        version = _ensure_loadable(binary, emit)
    except RuntimeError:
        # a binary this system cannot load must not look like an installation
        shutil.rmtree(dest, ignore_errors=True)
        raise
    if version:
        emit(99, version)
    emit(100, f"llama.cpp installiert: {binary}", state="done")
    return str(binary)


def _extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a release archive — .zip on Windows, .tar.gz on Linux and macOS."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return
    with tarfile.open(archive) as tf:
        try:
            # the Linux build is a set of .so files reached through symlinks
            tf.extractall(dest, filter="data")
        except TypeError:  # Python without extraction filters (< 3.11.4)
            tf.extractall(dest)


def _ensure_loadable(binary: Path, emit: Any) -> str:
    """Make the fresh binary run, installing the libraries it is missing.

    The dynamic linker names one missing library per attempt, so this runs in
    a loop: install, try again, install what the next attempt reports. When
    nothing can be installed — no package manager, no root, or a library that
    is present but too old — the loader failure is raised and the caller
    discards the installation. Returns what the binary says about itself.
    """
    for _ in range(len(_LINUX_LIBRARIES)):
        try:
            return _verify_binary(binary)
        except _LoaderFailure as failure:
            if not failure.missing:
                raise
            emit(97, f"Fehlende Systembibliothek: {', '.join(failure.missing)}")
            installed = _install_system_libraries(failure.missing, emit)
            if not installed:
                raise
            logger.info("installed system packages: %s", ", ".join(installed))
            emit(98, f"Systempaket installiert: {', '.join(installed)}")
    return _verify_binary(binary)


def _install_system_libraries(libraries: list[str], emit: Any) -> list[str]:
    """Install the packages carrying `libraries`; returns what was installed.

    Only on Linux, and only when this process can act as root — a systemd
    service usually can, a desktop start cannot. sudo is called
    non-interactively, so nothing ever waits for a password nobody can type,
    and the package names come from _LINUX_LIBRARIES, never from the output
    that was parsed.
    """
    if platform.system() != "Linux":
        return []
    manager = _package_manager()
    if manager is None:
        logger.info("no package manager available as root — cannot install %s", libraries)
        return []
    installed: list[str] = []
    refreshed = False
    for library in libraries:
        for package in _LINUX_LIBRARIES.get(library, {}).get(manager.key, ()):
            emit(97, f"Installiere fehlendes Systempaket {package} ({manager.key}) ...")
            if _run_privileged([*manager.install, package]):
                installed.append(package)
                break
            if manager.refresh and not refreshed:
                # a container image often ships without any package lists
                refreshed = True
                emit(97, "Aktualisiere die Paketlisten ...")
                if _run_privileged(manager.refresh) and _run_privileged(
                    [*manager.install, package]
                ):
                    installed.append(package)
                    break
            emit(97, f"{package} ließ sich nicht installieren")
    return installed


class _Manager(NamedTuple):
    """One package manager, ready to call: `install` takes a package name."""

    key: str
    install: list[str]
    refresh: list[str]


def _package_manager() -> _Manager | None:
    """The package manager to use, already prefixed with what makes it root."""
    prefix = _root_prefix()
    if prefix is None:
        return None
    for key, program, arguments, refresh in _PACKAGE_MANAGERS:
        path = shutil.which(program)
        if path:
            return _Manager(
                key, [*prefix, path, *arguments], [*prefix, path, *refresh] if refresh else []
            )
    return None


def _root_prefix() -> list[str] | None:
    """[] when this process is root, a non-interactive sudo when it may become it."""
    if getattr(os, "geteuid", lambda: 1)() == 0:
        return []
    sudo = shutil.which("sudo")
    if sudo is None:
        return None
    # -n never prompts: without the right this fails immediately, which is the answer
    if not _run_privileged([sudo, "-n", "true"], timeout=20):
        return None
    return [sudo, "-n"]


def _run_privileged(cmd: list[str], timeout: int = _PACKAGE_TIMEOUT_S) -> bool:
    """Run one package-manager command as root; True when it succeeded."""
    try:
        result = procutil.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("package command failed (%s): %s", exc, " ".join(cmd))
        return False
    if result.returncode != 0:
        logger.warning(
            "package command exited %s: %s\n%s",
            result.returncode,
            " ".join(cmd),
            (result.stderr or result.stdout or "").strip()[:2000],
        )
        return False
    return True


def _installable_libraries(output: str) -> list[str]:
    """Absent libraries a package manager could supply, as the loader named them.

    A version mismatch (`GLIBCXX_3.4.29' not found) answers with nothing: the
    library is there, the distribution is simply older than the build machine.
    """
    lowered = output.lower()
    if "glibc_" in lowered or "glibcxx_" in lowered:
        return []
    found = [name for name in _MISSING_LIBRARY.findall(lowered) if name in _LINUX_LIBRARIES]
    return list(dict.fromkeys(found))


def _verify_binary(binary: Path) -> str:
    """Run the fresh binary once, so a missing system library surfaces here.

    The Linux archive links against libstdc++/libgomp of the host, and a
    distribution older than the build machine has neither in a new enough
    version. Without this check that would only show up much later, when the
    first AI step tries to start the server. The GPU backend is loaded lazily
    by ggml and is deliberately not part of this check.
    """
    try:
        result = procutil.run(
            [str(binary), "--version"],
            cwd=str(binary.parent),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
    except OSError as exc:
        logger.error("llama-server is not executable: %s", exc)
        raise RuntimeError(f"llama-server ist nicht ausführbar: {exc}") from exc
    except subprocess.TimeoutExpired:
        return ""  # it started and kept running, which is all this checks
    output = f"{result.stdout}\n{result.stderr}"
    failure = _loader_failure(result.returncode, output)
    if failure:
        logger.error("llama-server cannot run (exit %s): %s", result.returncode, failure)
        raise _LoaderFailure(
            f"llama-server lässt sich auf diesem System nicht starten: {failure}",
            _installable_libraries(output),
        )
    logger.info("llama-server verified: %s", output.strip().splitlines()[:1])
    return next((line.strip() for line in output.splitlines() if line.strip()), "")


def _loader_failure(returncode: int, output: str) -> str:
    """Why the system cannot load this binary — empty when it ran."""
    if returncode in (_WIN_DLL_NOT_FOUND, _WIN_DLL_NOT_FOUND - 0x100000000):
        return "eine benötigte DLL fehlt (CUDA-Laufzeit oder Visual-C++-Runtime)"
    lowered = output.lower()
    if not any(marker in lowered for marker in _LOADER_ERRORS):
        return ""
    detail = next((line.strip() for line in output.splitlines() if line.strip()), "")
    return f"{_linux_hint(lowered)}{detail}" if platform.system() == "Linux" else detail


def _linux_hint(lowered: str) -> str:
    """What an admin has to do about a failed load — the log is English, this is not.

    The official release is built on Ubuntu and needs glibc 2.34 and
    libstdc++ from GCC 11 (Debian 12, Ubuntu 22.04 and newer); an older
    distribution cannot run it at all, while a libstdc++ that is merely
    absent is one package away.
    """
    too_old = (
        "die Distribution ist zu alt für das offizielle llama.cpp-Release "
        "(nötig sind glibc 2.34 und libstdc++ aus GCC 11, also z. B. Debian 12 "
        "oder Ubuntu 22.04): "
    )
    if "glibcxx_" in lowered or "glibc_" in lowered:
        return too_old
    packages = [_LINUX_LIBRARIES[library]["apt"][0] for library in _installable_libraries(lowered)]
    if packages:
        names = " ".join(dict.fromkeys(packages))
        return f"es fehlen Systempakete (apt install {names}): "
    return ""


def _phase(emit: Any, base: int, span: int, label: str) -> Any:
    """Map one download's 0-98 progress into its slice of the overall bar."""

    def report(percent: int, message: str, state: str = "running") -> None:
        emit(base + percent * span // 100, message or label, state)

    return report


def _require_free_space(target: Path, needed_bytes: int) -> None:
    """Refuse up front instead of dying with a full disk mid-download."""
    if needed_bytes <= 0:
        return
    try:
        free = shutil.disk_usage(target).free
    except OSError:  # the directory does not exist yet, or is not a real mount
        return
    if free < needed_bytes:
        raise RuntimeError(
            f"Zu wenig Speicherplatz in {target}: "
            f"{needed_bytes // (1024 * 1024)} MB nötig, {free // (1024 * 1024)} MB frei"
        )


class _Interrupted(RuntimeError):
    """A download that stopped early — worth another attempt, unlike an HTTP error."""


def _download_file(url: str, target: Path, max_bytes: int, emit: Any) -> None:
    """Download `url` to `target`, continuing after a dropped connection.

    A GGUF model is several gigabytes, and a connection that drops at 90 %
    used to mean starting over. Every attempt after the first asks for the
    rest of the file (HTTP Range), so only the missing part is fetched again.
    A refusal from the server (404, 401 …) is final and not retried.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        have = target.stat().st_size if target.exists() else 0
        try:
            _download_stream(url, target, max_bytes, emit, have)
            return
        except (_Interrupted, httpx.TransportError) as exc:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            logger.warning("download attempt %d failed (%s), continuing: %s", attempt, exc, url)
            emit(0, f"Verbindung abgebrochen — neuer Versuch {attempt + 1}/{DOWNLOAD_ATTEMPTS} ...")
            time.sleep(DOWNLOAD_RETRY_DELAY_S)


def _download_stream(url: str, target: Path, max_bytes: int, emit: Any, have: int) -> None:
    """One attempt: append to `target` from byte `have` on."""
    headers = {"Range": f"bytes={have}-"} if have else {}
    with httpx.stream("GET", url, follow_redirects=True, timeout=120, headers=headers) as response:
        if response.status_code == 416:
            # the partial file is at or past the end — it does not belong to this URL
            target.unlink(missing_ok=True)
            raise _Interrupted(f"Teildatei passt nicht zum Download: {url}")
        if have and response.status_code == 200:
            have = 0  # the server ignored the range and sends the whole file
        response.raise_for_status()
        remaining = int(response.headers.get("content-length", 0))
        total = have + remaining
        if total > max_bytes:
            raise RuntimeError(f"Download zu groß ({total} Bytes): {url}")
        _require_free_space(target.parent, remaining)
        received = have
        last_percent = -1
        with open(target, "ab" if have else "wb") as fh:
            for chunk in response.iter_bytes(chunk_size=1 << 18):
                received += len(chunk)
                if received > max_bytes:
                    raise RuntimeError(f"Download überschreitet Größenlimit: {url}")
                fh.write(chunk)
                if total:
                    percent = int(received * 98 / total)
                    if percent != last_percent:
                        last_percent = percent
                        emit(percent, "")
    if total and received != total:
        # a connection dropping mid-stream must not leave a file that looks complete
        raise _Interrupted(f"Download unvollständig ({received} von {total} Bytes): {url}")


# ── GGUF model downloads ──────────────────────────────────────────────


def list_installed_models() -> list[dict[str, Any]]:
    result = []
    for path in sorted(llm_models_dir().glob("*.gguf")):
        result.append({"file": path.name, "size_mb": path.stat().st_size // (1024 * 1024)})
    return result


def start_model_download(name: str) -> bool:
    entry = next((m for m in MODEL_CATALOG if m["name"] == name or m["file"] == name), None)
    if entry is None:
        raise ValueError(f"Unknown LLM model: {name}")

    with _download_lock:
        if entry["name"] in _downloads_running:
            return False
        _downloads_running.add(entry["name"])

    def emit(percent: int, message: str, state: str = "running") -> None:
        hub.publish(
            "model.download",
            {
                "scope": "llm",
                "name": entry["name"],
                "state": state,
                "percent": percent,
                "detail": message,
            },
        )

    def worker() -> None:
        target = llm_models_dir() / entry["file"]
        partial = target.with_suffix(".part")
        try:
            emit(0, f"Lade {entry['file']} ({entry['size_mb']} MB) ...")
            # same here: a part file from an earlier process is not resumed blindly
            partial.unlink(missing_ok=True)
            _download_file(entry["url"], partial, MAX_MODEL_BYTES, emit)
            _check_gguf(partial)
            partial.replace(target)
            emit(100, f"{entry['file']} geladen: {target}", state="done")
        except Exception as exc:
            logger.exception("LLM model download failed: %s", entry["name"])
            partial.unlink(missing_ok=True)
            emit(0, _download_error(exc), state="error")
        finally:
            with _download_lock:
                _downloads_running.discard(entry["name"])

    threading.Thread(target=worker, daemon=True, name=f"llm-download-{entry['name']}").start()
    return True


def _check_gguf(path: Path) -> None:
    """A GGUF file starts with its magic — an error page saved as .gguf does not."""
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic != b"GGUF":
        raise RuntimeError("Die heruntergeladene Datei ist kein GGUF-Modell")


def _download_error(exc: Exception) -> str:
    """German wording for a failed download; httpx phrases its own in English."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (403, 429) and "api.github.com" in str(exc.request.url):
            return "GitHub blockt die Abfrage gerade (Ratenlimit) — bitte später erneut versuchen"
        if status in (401, 403):
            return f"Download nicht möglich — die Quelle verweigert den Zugriff (HTTP {status})"
        return f"Download nicht möglich (HTTP {status})"
    if isinstance(exc, httpx.HTTPError):
        return f"Download fehlgeschlagen: Verbindungsfehler ({type(exc).__name__})"
    return str(exc)


def delete_model(filename: str) -> None:
    target = (llm_models_dir() / Path(filename).name).resolve()
    if target.parent != llm_models_dir().resolve() or target.suffix != ".gguf":
        raise ValueError("Invalid model name")
    target.unlink(missing_ok=True)


# ── managed server ────────────────────────────────────────────────────

_server_lock = threading.Lock()
_server_process: subprocess.Popen | None = None
_server_model: str = ""


def active_model_name() -> str:
    return _server_model


def _pick_model_file() -> Path:
    configured = config.get_settings().llm.model
    if configured:
        candidate = llm_models_dir() / Path(configured).name
        if candidate.exists():
            return candidate
    installed = list_installed_models()
    if not installed:
        raise RuntimeError("No local LLM model installed — download one in Settings")
    recommended = recommend_model()
    for model in installed:
        if model["file"] == recommended["file"]:
            return llm_models_dir() / model["file"]
    return llm_models_dir() / installed[0]["file"]


def file_needs_mb(model_file: Path) -> int:
    """Memory the GGUF on disk needs — from the catalog, else from its size."""
    entry = next((m for m in MODEL_CATALOG if m["file"] == model_file.name), None)
    if entry is not None:
        return model_needs_mb(entry)
    try:
        size_mb = model_file.stat().st_size // (1024 * 1024)
    except OSError:
        size_mb = 0
    return hardware.gguf_requirement(size_mb)


def _drain_stderr(process: subprocess.Popen) -> list[str]:
    """Keep the last llama-server lines: they say *why* it died.

    Without this the output went to DEVNULL and an OOM abort was
    indistinguishable from any other crash.
    """
    tail: list[str] = []
    if process.stderr is None:
        return tail

    def reader() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            tail.append(line.rstrip())
            del tail[:-40]

    threading.Thread(target=reader, daemon=True, name="llama-stderr").start()
    return tail


def ensure_running() -> str:
    """Start llama-server if needed; returns the OpenAI-compatible base URL.

    Refuses before starting when the model cannot fit anywhere, and keeps the
    layers in RAM when they do not fit into the free VRAM — offloading a model
    that is too large is what made llama-server die on startup.
    """
    global _server_process, _server_model
    with _server_lock:
        base_url = f"http://127.0.0.1:{SERVER_PORT}/v1"
        if _server_process is not None and _server_process.poll() is None:
            return base_url

        binary = server_binary()
        if binary is None:
            raise RuntimeError("llama.cpp ist nicht installiert — in den Einstellungen einrichten")
        model_file = _pick_model_file()

        needs_mb = file_needs_mb(model_file)
        hw = hardware.probe(fresh=True)
        verdict = hardware.check_llm_model(needs_mb, hw=hw)
        if verdict["level"] == hardware.NO:
            raise hardware.InsufficientMemory(
                f"Das Modell '{model_file.name}' passt nicht in den Speicher. {verdict['message']}"
            )
        on_gpu = hardware.offload_to_gpu(needs_mb, hw)
        if hardware.has_gpu(hw) and not on_gpu:
            logger.info(
                "%s (%d MB) does not fit the free VRAM (%d MB) — keeping it in RAM",
                model_file.name,
                needs_mb,
                hw["vram_free_mb"],
            )

        cmd = [
            str(binary),
            "-m",
            str(model_file),
            "--host",
            "127.0.0.1",
            "--port",
            str(SERVER_PORT),
            "--ctx-size",
            "16384",
            "-ngl",
            "999" if on_gpu else "0",
        ]
        logger.info("starting llama-server: %s", " ".join(cmd))
        hub.publish(
            "engine.status",
            {"engine": "llm", "state": "loading", "detail": model_file.name},
        )
        _server_process = procutil.popen(
            cmd,
            cwd=str(binary.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        _server_model = model_file.stem
        tail = _drain_stderr(_server_process)

        deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if _server_process.poll() is not None:
                _server_process = None
                raise RuntimeError(_startup_failure(model_file, tail, on_gpu))
            try:
                response = httpx.get(f"http://127.0.0.1:{SERVER_PORT}/health", timeout=2)
                if response.status_code == 200:
                    hub.publish(
                        "engine.status",
                        {"engine": "llm", "state": "idle", "detail": model_file.name},
                    )
                    return base_url
            except httpx.HTTPError:
                pass
            time.sleep(1)

        stop_server()
        raise RuntimeError("llama-server wurde nicht rechtzeitig bereit")


def _startup_failure(model_file: Path, tail: list[str], on_gpu: bool) -> str:
    """German explanation for a server that quit during startup."""
    output = "\n".join(tail)
    logger.error("llama-server exited during startup:\n%s", output or "(no output)")
    if hardware.is_oom(output):
        return (
            f"Der lokale KI-Server konnte '{model_file.name}' nicht laden: "
            + hardware.oom_message("gpu" if on_gpu else "cpu")
            + " Bitte ein kleineres Modell wählen."
        )
    loader = _loader_failure(0, output)
    if loader:
        # an installation from before the dependency check, or a package removed since
        return f"llama-server lässt sich nicht starten: {loader}"
    last = next((line for line in reversed(tail) if line.strip()), "")
    detail = f" ({last})" if last else ""
    return f"llama-server wurde unerwartet beendet{detail}"


def stop_server() -> None:
    """Stop the managed server (frees VRAM/RAM for Whisper)."""
    global _server_process, _server_model
    with _server_lock:
        if _server_process is None:
            return
        logger.info("stopping llama-server")
        _server_process.terminate()
        try:
            _server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server_process.kill()
        _server_process = None
        _server_model = ""
        hub.publish("engine.status", {"engine": "llm", "state": "stopped", "detail": ""})


def status() -> dict[str, Any]:
    hw = probe_hardware()
    catalog = [{**entry, "fit": fit_for(entry, hw)} for entry in MODEL_CATALOG]
    installed = list_installed_models()
    for model in installed:
        model["fit"] = hardware.check_llm_model(hardware.gguf_requirement(model["size_mb"]), hw=hw)
    return {
        "binary_installed": server_binary() is not None,
        "install": install_state(),
        "server_running": _server_process is not None and _server_process.poll() is None,
        "active_model": _server_model,
        "hardware": hw,
        # what a model could occupy here — for an endpoint on localhost, which
        # Verba does not manage (the UI shows it as an estimate, not a verdict)
        "budget": hardware.model_budget(hw),
        "recommended": recommend_model(hw),
        "catalog": catalog,
        "installed": installed,
        "models_dir": str(llm_models_dir()),
    }
