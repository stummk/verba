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

import json
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
from . import download, hardware

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


#: An installation whose backend was measured rather than recorded, remembered
#: for this process: (binary, verdict). See `ensure_backend_recorded`.
_measured_backend: tuple[Path, dict[str, Any]] | None = None


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
    candidates = [path for path in binary_dir().rglob(_server_exe()) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _server_exe() -> str:
    return "llama-server.exe" if platform.system() == "Windows" else "llama-server"


def _server_binary_in(directory: Path) -> Path | None:
    """The llama-server of one candidate — not of whichever attempt is newest."""
    return next((path for path in directory.rglob(_server_exe()) if path.is_file()), None)


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
    # the Vulkan build's way to the GPU: the loader is a package, the driver's
    # ICD next to it is not — a container without it starts and sees no card
    "libvulkan.so.1": {
        "apt": ("libvulkan1",),
        "dnf": ("vulkan-loader",),
        "zypper": ("libvulkan1",),
        "pacman": ("vulkan-icd-loader",),
        "apk": ("vulkan-loader",),
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


class _Candidate(NamedTuple):
    """One installable llama.cpp build.

    `key` is both the directory it unpacks into and the backend name the
    status reports. `gpu` says whether this build is *meant* to reach the
    graphics card — whether it actually does is a question only the installed
    binary can answer (`_probe_devices`), and the whole ladder exists because
    the two are not the same thing.
    """

    key: str
    pattern: str
    gpu: bool
    label: str


def _candidates() -> list[_Candidate]:
    """The builds to try on this system, best first.

    "GPU support" is a different package on every platform. Windows has
    official CUDA builds — CUDA 12 first, because it still works with older
    drivers than the CUDA 13 one. For Linux llama.cpp publishes no CUDA build
    at all, only a Vulkan one: that reaches an NVIDIA card through the
    driver's ICD, costs 30 MB instead of a toolkit, and needs nothing but the
    Vulkan loader — which `_ensure_loadable` installs like any other missing
    library. Where even that finds no card, `llamabuild` compiles a CUDA build
    on the machine.

    The plain CPU build closes the ladder, so llama.cpp stays usable on a
    system that has no GPU or cannot be brought to use it.
    """
    system = platform.system()
    arch = _arch_token()
    if not arch:
        return []
    gpu = hardware.has_gpu()
    if system == "Windows":
        ladder = []
        if gpu and not _is_arm():
            ladder.append(_Candidate("cuda", "bin-win-cuda-12", True, "CUDA-Build"))
        if gpu:
            ladder.append(_Candidate("cuda", "bin-win-cuda", True, "CUDA-Build"))
        ladder.append(_Candidate("cpu", f"bin-win-cpu-{arch}", False, "CPU-Build"))
        return ladder
    if system == "Linux":
        ladder = []
        if gpu:
            ladder.append(_Candidate("vulkan", f"bin-ubuntu-vulkan-{arch}", True, "Vulkan-Build"))
        ladder.append(_Candidate("cpu", f"bin-ubuntu-{arch}", False, "CPU-Build"))
        return ladder
    if system == "Darwin":
        # Metal is compiled into the official build; nothing to choose here
        return [_Candidate("cpu", f"bin-macos-{arch}", False, "macOS-Build")]
    return []


def _pick_candidates(assets: list[dict[str, Any]]) -> list[_Rung]:
    """Every candidate this release actually carries an archive for.

    Windows ships .zip, Linux and macOS .tar.gz — both are accepted, so a
    change of packaging on one platform does not break the other. The
    architecture is required on top of the pattern: a release that drops one
    CUDA version would otherwise let the broader "bin-win-cuda" fragment match
    the arm64 build on an x64 machine. An asset already taken by an earlier,
    more specific candidate is not offered twice.
    """
    arch = _arch_token()
    picked: list[_Rung] = []
    taken: set[str] = set()
    for candidate in _candidates():
        for asset in assets:
            name = asset.get("name", "")
            if name.startswith("cudart-"):
                continue  # the CUDA runtime, fetched alongside the build it belongs to
            if candidate.pattern in name and arch in name and name.endswith(ARCHIVE_SUFFIXES):
                if name not in taken:
                    taken.add(name)
                    picked.append((candidate, asset))
                break
    return picked


#: One rung of the installation ladder: the build and the archive it comes in
#: — or no archive, for the rung that is compiled here rather than downloaded.
_Rung = tuple[_Candidate, dict[str, Any] | None]

#: The rung `llamabuild` compiles. Not a candidate `_candidates()` can offer:
#: it has no asset, and whether it is possible is llamabuild's question.
_SOURCE_BUILD = _Candidate("cuda-source", "", True, "CUDA-Build aus den Quellen")


class _Attempt(NamedTuple):
    """What came of one rung.

    `binary` is None when the rung did not yield a usable installation, and
    `problem` then says why in German. `devices` is the three-valued answer of
    `_probe_devices`: cards found, none found, or could not be asked.
    """

    binary: Path | None
    devices: list[str] | None = None
    problem: str = ""


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


def resolve_release() -> tuple[dict[str, Any], list[_Rung]]:
    """Newest release carrying builds for this platform, and that ladder.

    The release layout changed: `releases/latest` now answers with a semver
    release whose only asset is `nightly-tag.txt`, naming the nightly build
    that has the binaries. That pointer is followed, a release carrying the
    assets itself is still used directly, and if neither does, the release
    list is scanned from the top.
    """
    latest = _get_json(RELEASE_API)
    releases = [latest]
    pointer = next((a for a in latest.get("assets", []) if a.get("name") == NIGHTLY_POINTER), None)
    if pointer is not None:
        tag = _fetch_text(pointer["browser_download_url"]).strip()
        if tag:
            releases.append(_get_json(RELEASE_TAG_API.format(tag=tag)))
    for release in releases:
        rungs = _pick_candidates(release.get("assets", []))
        if rungs:
            return release, rungs

    for release in _get_json(RELEASE_LIST_API) or []:
        if not isinstance(release, dict):
            continue
        rungs = _pick_candidates(release.get("assets", []))
        if rungs:
            return release, rungs
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


def start_binary_install(force: bool = False) -> bool:
    """Install the llama.cpp binary in a background thread (API entry point)."""
    with _download_lock:
        if "llama.cpp" in _downloads_running:
            return False
        _downloads_running.add("llama.cpp")
    _install_state.update(running=True, percent=0, detail="", error="", log=[])

    def run() -> None:
        try:
            install_binary(force=force)
        except Exception as exc:
            logger.exception("llama.cpp installation failed")
            _install_event(0, download.error_message(exc), state="error")
        finally:
            _install_state["running"] = False
            with _download_lock:
                _downloads_running.discard("llama.cpp")

    threading.Thread(target=run, daemon=True, name="llama-setup").start()
    return True


def install_binary(report: Any = None, force: bool = False) -> str:
    """Install llama.cpp so that it uses the GPU wherever the machine has one.

    Not one download but a ladder, walked until a build is *proven* to see the
    card: the GPU package for this platform first, then — on Linux, where
    llama.cpp publishes no CUDA build — a CUDA build compiled on the machine,
    and the CPU build last. Every rung is installed, asked what devices it
    sees, and removed again when the answer is none, so what stays behind is a
    single installation whose backend is known instead of a binary that
    quietly computes on the processor.
    """

    def emit(percent: int, message: str, state: str = "running") -> None:
        _install_event(percent, message, state)
        if report:
            report(percent, message)

    existing = server_binary()
    if existing is not None and not force:
        emit(100, f"llama.cpp ist bereits installiert: {existing}", state="done")
        return str(existing)
    if existing is not None:
        # Asked for again on purpose — usually to replace a build that computes
        # on the processor. The old installation is deliberately *not* removed
        # here: a release that cannot be reached, a download that fails, a
        # build that does not run — every one of those would otherwise leave
        # this machine with no llama.cpp at all. `_accept_install` drops it
        # once a replacement has proven itself; until then it stays and works.
        # Only the running server has to let go, so its files can be replaced.
        emit(0, "Ersetze die vorhandene Installation ...")
        stop_server()

    emit(0, f"System: {platform.system()} {platform.machine()}")
    emit(0, "Suche aktuelles llama.cpp-Release ...")
    release, rungs = resolve_release()
    tag = release.get("tag_name", "?")
    logger.info("installing llama.cpp %s: %s", tag, [rung[1]["name"] for rung in rungs])
    plan = ", ".join(f"{candidate.label} ({asset['name']})" for candidate, asset in rungs)
    emit(2, f"Release {tag}, Paket(e): {plan}")

    from . import llamabuild

    can_build, build_reason = llamabuild.possible()
    ladder = _ladder(rungs, can_build)
    problems: list[str] = []
    if not can_build and build_reason:
        emit(4, f"Kein CUDA-Build aus den Quellen: {build_reason}")
        problems.append(f"CUDA-Build aus den Quellen: {build_reason}")

    # every attempt owns an equal slice, so the bar only moves forward
    span = max(85 // max(len(ladder), 1), 1)
    cursor = 5

    for candidate, asset in ladder:
        try:
            attempt = _try_candidate(candidate, asset, release, emit, cursor, span)
        except Exception as exc:  # noqa: BLE001 — one rung failing is not the end
            # A rung can fail for reasons that say nothing about the next one:
            # too little disk space for a CUDA runtime, an archive over the
            # size limit, a download that breaks off. Letting that escape
            # would skip the 17 MB CPU build over the 600 MB one — so it is a
            # failed attempt like any other, and the ladder walks on.
            logger.exception("%s could not be installed", candidate.label)
            attempt = _Attempt(None, problem=download.error_message(exc))
        cursor += span
        if attempt.binary is None:
            problems.append(f"{candidate.label}: {attempt.problem}")
            emit(cursor, f"{candidate.label}: {attempt.problem}")
            continue
        # a GPU build has to show a card; one that could not be asked is
        # trusted, and the CPU build has nothing to show in the first place
        if attempt.devices or attempt.devices is None or not candidate.gpu:
            if problems and not candidate.gpu:
                emit(cursor, "Es bleibt beim CPU-Build: " + " · ".join(problems))
            return _accept_install(attempt.binary, candidate, attempt.devices, emit)
        problems.append(f"{candidate.label}: findet auf diesem System keine Grafikkarte")
        emit(cursor, f"{candidate.label} findet keine Grafikkarte — nächste Möglichkeit ...")
        shutil.rmtree(binary_dir() / candidate.key, ignore_errors=True)

    detail = " · ".join(problems) or "kein passendes Paket"
    logger.error("no llama.cpp build works here: %s", detail)
    raise RuntimeError(f"llama.cpp ließ sich hier nicht installieren: {detail}")


def _ladder(rungs: list[_Rung], can_build: bool) -> list[_Rung]:
    """The rungs to walk, with the compiled one in its place.

    The build from source belongs after every prebuilt GPU package — it is by
    far the most expensive way to the card — and before the CPU build, which
    is the giving-up rung. It carries no asset: that is how `_try_candidate`
    knows to compile instead of download.
    """
    if not can_build:
        return list(rungs)
    ladder = list(rungs)
    ladder.insert(
        next((index for index, (c, _) in enumerate(ladder) if not c.gpu), len(ladder)),
        (_SOURCE_BUILD, None),
    )
    return ladder


def _try_candidate(
    candidate: _Candidate,
    asset: dict[str, Any] | None,
    release: dict[str, Any],
    emit: Any,
    base: int,
    span: int,
) -> _Attempt:
    """Install one rung and ask it what it sees.

    Every candidate gets its own directory, and one that cannot run here is
    deleted again: a rejected attempt must leave nothing behind that
    `server_binary()` would later find and start. A rung without an asset is
    compiled rather than downloaded.
    """
    if asset is None:
        return _build_from_source(candidate, release, emit, base, span)

    dest = binary_dir() / candidate.key
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    downloads = [(asset, candidate.label)]
    cudart = _pick_cudart_asset(release.get("assets", []), asset["name"])
    if cudart is not None:
        downloads.append((cudart, "CUDA-Laufzeit"))
    # archive plus unpacked content, and only the compressed size is known
    download.require_free_space(dest, sum(int(item.get("size", 0)) for item, _ in downloads) * 3)

    share = max(span // len(downloads), 1)
    for index, (item, label) in enumerate(downloads):
        size = int(item.get("size", 0))
        if size > MAX_BINARY_BYTES:
            raise RuntimeError(f"{label}-Download zu groß ({size} Bytes)")
        start = base + index * share
        archive = dest / item["name"]
        # a leftover from an earlier run cannot be verified — only resume within this one
        archive.unlink(missing_ok=True)
        emit(start, f"Lade {label} ({size // (1024 * 1024)} MB) ...")
        download.fetch(
            item["browser_download_url"],
            archive,
            MAX_BINARY_BYTES,
            download.phase(emit, start, share, f"Lade {label} ..."),
        )
        emit(start + share, f"Entpacke {label} ...")
        _extract_archive(archive, dest)
        archive.unlink(missing_ok=True)

    binary = _server_binary_in(dest)
    if binary is None:
        logger.error("no llama-server in %s", asset["name"])
        shutil.rmtree(dest, ignore_errors=True)
        return _Attempt(None, problem=f"im Archiv {asset['name']} war kein llama-server")
    if platform.system() != "Windows":
        binary.chmod(0o755)

    emit(base + span, f"Prüfe, ob {candidate.label} auf diesem System startet ...")
    try:
        version = _ensure_loadable(binary, emit, base + span)
    except RuntimeError as exc:
        logger.warning("%s does not run here: %s", candidate.label, exc)
        shutil.rmtree(dest, ignore_errors=True)
        return _Attempt(None, problem=str(exc))
    if version:
        emit(base + span, version)
    # only a GPU build has a decision riding on this — for the CPU build the
    # answer is known, and asking would be a process spawn for nothing
    return _Attempt(binary, _probe_devices(binary) if candidate.gpu else [])


def _build_from_source(
    candidate: _Candidate, release: dict[str, Any], emit: Any, base: int, span: int
) -> _Attempt:
    """Compile the rung that has no package — see services/llamabuild.py.

    Whatever the build raises reaches `install_binary`'s loop, which treats it
    as a rung that did not work and walks on to the CPU build — the whole
    point of having one.
    """
    from . import llamabuild

    emit(base, f"Baue llama.cpp mit CUDA auf dieser Maschine ({candidate.label}) ...")
    binary = llamabuild.build(release.get("tag_name", "?"), emit, base=base, span=span)
    return _Attempt(binary, _probe_devices(binary))


def _accept_install(
    binary: Path, candidate: _Candidate, devices: list[str] | None, emit: Any
) -> str:
    """Keep this build, drop the other attempts, and record what it is."""
    try:
        for entry in binary_dir().iterdir():
            if entry.is_dir() and entry.name != candidate.key:
                shutil.rmtree(entry, ignore_errors=True)
    except OSError:  # nothing to clean up is not a failure
        pass
    global _measured_backend
    _measured_backend = None
    # a fresh GPU build that could not be asked is trusted; see _probe_devices
    _record_backend(candidate.key, devices, gpu=candidate.gpu)
    where = f" — Grafikkarte: {devices[0]}" if devices else ""
    logger.info("llama.cpp installed: %s (%s)%s", binary, candidate.key, where)
    emit(100, f"llama.cpp installiert: {candidate.label}{where}", state="done")
    return str(binary)


def _backend_marker() -> Path:
    return binary_dir() / "backend.json"


def _record_backend(
    key: str, devices: list[str] | None, *, gpu: bool, persist: bool = True
) -> dict[str, Any]:
    """Write down which build is installed and what it saw; returns that.

    `gpu` is what to believe when the devices could not be asked at all: a
    build just installed *as* a GPU build gets the benefit of the doubt, an
    installation nobody measured before does not.
    """
    backend = {
        "backend": key,
        "gpu": bool(devices) if devices is not None else gpu,
        "verified": devices is not None,
        "devices": devices or [],
    }
    if persist:
        try:
            _backend_marker().write_text(json.dumps(backend, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:  # the installation is fine, only the note is not
            logger.warning("could not record the llama.cpp backend: %s", exc)
    return backend


def installed_backend() -> dict[str, Any]:
    """Which build is installed and which GPUs it reported when it was.

    Written once, when a build is accepted, because probing the devices again
    would be a process spawn on every status poll — and the answer only
    changes when the installation or the driver does.
    """
    try:
        data = json.loads(_backend_marker().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _backend_from_devices(devices: list[str]) -> str:
    for device in devices:
        for prefix, key in _GPU_BACKENDS.items():
            if device.startswith(prefix):
                return key
    return ""


def ensure_backend_recorded() -> dict[str, Any]:
    """The installed build's backend, measured once where it was never recorded.

    An installation from before this was written down is the interesting case:
    the binary is there and nobody — including its owner — knows whether it
    ever reaches the graphics card. So it is asked, the answer is stored, and
    the status can say "CPU" out loud instead of only "installed". One process
    spawn, once, not on every poll.
    """
    global _measured_backend
    known = installed_backend()
    if known:
        return known
    binary = server_binary()
    if binary is None:
        return {}
    if _measured_backend is not None and _measured_backend[0] == binary:
        return dict(_measured_backend[1])
    devices = _probe_devices(binary)
    key = _backend_from_devices(devices or [])
    # A binary that could not be asked at all must not be written down as the
    # answer: one timed-out probe would brand a working CUDA installation as
    # "unknown" for good, with a reinstall the only way out. So an uncertain
    # verdict lives in this process only — the next start asks again — while
    # the certain one is stored and never re-probed. Either way it is measured
    # once, not once per status poll, which a failed write used to cost.
    backend = _record_backend(
        key or ("unknown" if devices is None else "cpu"),
        devices,
        gpu=False,
        persist=devices is not None,
    )
    _measured_backend = (binary, backend)
    return backend


def _backend_status(hw: dict[str, Any]) -> dict[str, Any]:
    """The installed build, plus whether replacing it with a GPU one is worth it.

    `upgradable` is a decision, not a fact, and it is made here rather than in
    the UI: this is the only place that knows both what is installed and what
    the machine has. The frontend only has to ask whether to show the offer.
    """
    backend = ensure_backend_recorded()
    if not backend:
        return backend
    return {**backend, "upgradable": hardware.has_gpu(hw) and not backend["gpu"]}


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


def _ensure_loadable(binary: Path, emit: Any, percent: int = 97) -> str:
    """Make the fresh binary run, installing the libraries it is missing.

    The dynamic linker names one missing library per attempt, so this runs in
    a loop: install, try again, install what the next attempt reports. When
    nothing can be installed — no package manager, no root, or a library that
    is present but too old — the loader failure is raised and the caller
    discards the installation. Returns what the binary says about itself.

    `percent` is where in the bar this happens: one rung of the installation
    ladder owns a slice, and a repair inside it must not report the 97 % that
    was right back when there was only ever one attempt.
    """
    for _ in range(len(_LINUX_LIBRARIES)):
        try:
            return _verify_binary(binary)
        except _LoaderFailure as failure:
            if not failure.missing:
                raise
            emit(percent, f"Fehlende Systembibliothek: {', '.join(failure.missing)}")
            installed = _install_system_libraries(failure.missing, emit, percent)
            if not installed:
                raise
            logger.info("installed system packages: %s", ", ".join(installed))
            emit(percent, f"Systempaket installiert: {', '.join(installed)}")
    return _verify_binary(binary)


def _install_system_libraries(libraries: list[str], emit: Any, percent: int = 97) -> list[str]:
    """Install the packages carrying the libraries the loader is missing."""
    return install_packages(_LINUX_LIBRARIES, libraries, emit, percent)


def can_install_packages() -> bool:
    """Whether system packages can be installed here at all — asked before
    a plan is announced that depends on it (services/llamabuild.py)."""
    return platform.system() == "Linux" and _package_manager() is not None


def install_packages(
    table: dict[str, dict[str, tuple[str, ...]]],
    names: list[str],
    emit: Any,
    percent: int = 97,
    timeout: int = _PACKAGE_TIMEOUT_S,
) -> list[str]:
    """Install the packages carrying `names`; returns what was installed.

    `table` maps what is missing — a library soname, a build tool — to the
    package that carries it per manager; only names from such a table are ever
    handed to a package manager, never anything parsed out of an output.

    Only on Linux, and only when this process can act as root — a systemd
    service usually can, a desktop start cannot. sudo is called
    non-interactively, so nothing ever waits for a password nobody can type.
    """
    if platform.system() != "Linux":
        return []
    manager = _package_manager()
    if manager is None:
        logger.info("no package manager available as root — cannot install %s", names)
        return []
    installed: list[str] = []
    refreshed = False
    for name in names:
        for package in table.get(name, {}).get(manager.key, ()):
            emit(percent, f"Installiere fehlendes Systempaket {package} ({manager.key}) ...")
            if _run_privileged([*manager.install, package], timeout):
                installed.append(package)
                break
            if manager.refresh and not refreshed:
                # a container image often ships without any package lists
                refreshed = True
                emit(percent, "Aktualisiere die Paketlisten ...")
                if _run_privileged(manager.refresh) and _run_privileged(
                    [*manager.install, package], timeout
                ):
                    installed.append(package)
                    break
            emit(percent, f"{package} ließ sich nicht installieren")
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


#: How ggml names a device that is not the CPU, and the backend behind it —
#: which is also how an installation that predates the marker is identified.
#: One table, because everything below is derived from it: three hand-kept
#: copies of these names had already drifted apart.
_GPU_BACKENDS = {
    "CUDA": "cuda",
    "Vulkan": "vulkan",
    "ROCm": "rocm",
    "HIP": "rocm",
    "SYCL": "sycl",
    "Metal": "metal",
    "MUSA": "musa",
}
_GPU_NAMES = "|".join(_GPU_BACKENDS)

#: A device line of `--list-devices`, e.g. "CUDA0: NVIDIA RTX A500 (4096 MiB)".
#: The CPU is a device too and deliberately not among these names.
_GPU_DEVICE = re.compile(rf"^\s*((?:{_GPU_NAMES})\d*\s*:\s*\S.*)$", re.MULTILINE)
#: What ggml prints while a backend initialises — a second witness, because a
#: build that lists no devices may still have found one this way.
_GPU_FOUND = re.compile(rf"found\s+([1-9]\d*)\s+(?:{_GPU_NAMES})", re.IGNORECASE)
#: An argument parser that does not know `--list-devices` says so like this.
_UNKNOWN_ARGUMENT = ("invalid argument", "unrecognized argument", "unknown argument", "error: --")


def _probe_devices(binary: Path) -> list[str] | None:
    """The GPUs this binary can actually use; `None` when it cannot be asked.

    This is the question the whole ladder turns on, and the only honest way to
    ask it is to let ggml load its backends and report: a CUDA build without a
    working driver and a Vulkan build without an ICD both start happily and
    then compute on the CPU. That silence is exactly what this breaks.

    An empty list is a definite "no GPU here". `None` means the binary did not
    understand the question — an ancient or a very new build — and the caller
    then has to decide on trust rather than on evidence.
    """
    try:
        result = procutil.run(
            [str(binary), "--list-devices"],
            cwd=str(binary.parent),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("could not list the devices of %s: %s", binary, exc)
        return None
    output = f"{result.stdout}\n{result.stderr}"
    devices = [match.group(1).strip() for match in _GPU_DEVICE.finditer(output)]
    if devices:
        logger.info("%s sees %s", binary.name, "; ".join(devices))
        return devices
    if _GPU_FOUND.search(output):
        # the backend announced a card without listing it as a device; where
        # the announcement wrapped over two lines there is nothing to name,
        # and that is "could not tell", not "no card"
        found = [line.strip() for line in output.splitlines() if _GPU_FOUND.search(line)]
        return found or None
    lowered = output.lower()
    if result.returncode != 0 and any(marker in lowered for marker in _UNKNOWN_ARGUMENT):
        logger.warning("%s does not know --list-devices", binary.name)
        return None
    logger.info("%s sees no GPU device", binary.name)
    return []


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
            download.fetch(entry["url"], partial, MAX_MODEL_BYTES, emit)
            _check_gguf(partial)
            partial.replace(target)
            emit(100, f"{entry['file']} geladen: {target}", state="done")
        except Exception as exc:
            logger.exception("LLM model download failed: %s", entry["name"])
            partial.unlink(missing_ok=True)
            emit(0, download.error_message(exc), state="error")
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
        # which build is installed and whether it reached the GPU: the one
        # thing a user cannot see from the outside (see install_binary). An
        # installation older than this bookkeeping is measured once here.
        "backend": _backend_status(hw),
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
