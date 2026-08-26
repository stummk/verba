"""Local LLM support via llama.cpp (`llama-server`).

One-click local setup, mirroring the ffmpeg approach:
- the llama.cpp release binary is downloaded into <data>/tools/llama/
- GGUF models are downloaded into the configured models directory
  (default <data>/models/llm) — any .gguf already lying there is used from
  there, so an existing collection needs no second download
- a hardware probe (RAM/VRAM) recommends a model + quantisation; the choice
  itself stays with the user
- `llama-server` runs as a managed subprocess speaking the OpenAI protocol,
  so the normal LLM client (services/llm.py) needs no special casing.

Progress is reported via "model.download" and "engine.status" events.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import re
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from .. import config
from ..events import hub

logger = logging.getLogger(__name__)

RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
MAX_BINARY_BYTES = 900 * 1024 * 1024  # CUDA builds bundle the runtime
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


# ── paths ─────────────────────────────────────────────────────────────


def binary_dir() -> Path:
    return config.tools_dir() / "llama"


def llm_models_dir() -> Path:
    """Where the GGUF files live — configurable, see config.llm_models_dir."""
    return config.llm_models_dir()


def server_binary() -> Path | None:
    exe = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    for candidate in binary_dir().rglob(exe):
        return candidate
    return None


# ── hardware probe ────────────────────────────────────────────────────


def _total_ram_mb() -> int:
    system = platform.system()
    try:
        if system == "Windows":

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
            return int(status.ullTotalPhys // (1024 * 1024))
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        match = re.search(r"MemTotal:\s+(\d+) kB", meminfo)
        return int(match.group(1)) // 1024 if match else 0
    except (OSError, AttributeError, ValueError):
        return 0


def _gpu_vram_mb() -> tuple[int, str]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            name, vram = out.stdout.strip().splitlines()[0].rsplit(",", 1)
            return int(vram.strip()), name.strip()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return 0, ""


def probe_hardware() -> dict[str, Any]:
    ram_mb = _total_ram_mb()
    vram_mb, gpu_name = _gpu_vram_mb()
    return {"ram_mb": ram_mb, "vram_mb": vram_mb, "gpu_name": gpu_name}


def recommend_model(hardware: dict[str, Any] | None = None) -> dict[str, Any]:
    """Largest fitting catalog model for the available VRAM (GPU) or half the RAM.

    Only `recommended` entries are suggested, so adding an alternative of the
    same size to the catalog does not silently change what new users get.
    """
    hw = hardware or probe_hardware()
    budget_mb = hw["vram_mb"] if hw["vram_mb"] > 0 else hw["ram_mb"] // 2
    fitting = [m for m in MODEL_CATALOG if m["min_free_mb"] <= budget_mb]
    preferred = [m for m in fitting if m.get("recommended")]
    return (preferred or fitting or [MODEL_CATALOG[0]])[-1]


# ── binary installation ───────────────────────────────────────────────


def _pick_release_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    system = platform.system()
    vram_mb, _ = _gpu_vram_mb()
    if system == "Windows":
        patterns = ["bin-win-cuda", "bin-win-cpu-x64"] if vram_mb else ["bin-win-cpu-x64"]
    elif system == "Linux":
        patterns = ["bin-ubuntu-x64"]
    else:
        return None
    for pattern in patterns:
        for asset in assets:
            name = asset.get("name", "")
            if pattern in name and name.endswith(".zip") and "cudart" not in name:
                return asset
    return None


def start_binary_install() -> bool:
    """Install the llama.cpp binary in a background thread (API entry point)."""
    with _download_lock:
        if "llama.cpp" in _downloads_running:
            return False
        _downloads_running.add("llama.cpp")

    def run() -> None:
        try:
            install_binary()
        except Exception as exc:
            logger.exception("llama.cpp installation failed")
            hub.publish(
                "model.download",
                {
                    "scope": "llm-binary",
                    "name": "llama.cpp",
                    "state": "error",
                    "percent": 0,
                    "detail": str(exc),
                },
            )
        finally:
            with _download_lock:
                _downloads_running.discard("llama.cpp")

    threading.Thread(target=run, daemon=True, name="llama-setup").start()
    return True


def install_binary(report: Any = None) -> str:
    """Download the current llama.cpp release binary; returns the server path."""

    def emit(percent: int, message: str, state: str = "running") -> None:
        hub.publish(
            "model.download",
            {
                "scope": "llm-binary",
                "name": "llama.cpp",
                "state": state,
                "percent": percent,
                "detail": message,
            },
        )
        if report:
            report(percent, message)

    existing = server_binary()
    if existing is not None:
        return str(existing)

    emit(0, "Suche aktuelles llama.cpp-Release ...")
    response = httpx.get(RELEASE_API, timeout=30, follow_redirects=True)
    response.raise_for_status()
    release = response.json()
    asset = _pick_release_asset(release.get("assets", []))
    if asset is None:
        raise RuntimeError(f"No llama.cpp binary found for {platform.system()}")

    size = int(asset.get("size", 0))
    if size > MAX_BINARY_BYTES:
        raise RuntimeError(f"llama.cpp-Download zu groß ({size} Bytes)")

    dest = binary_dir()
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / asset["name"]
    emit(0, f"Lade {asset['name']} ({size // (1024 * 1024)} MB) ...")
    _download_file(asset["browser_download_url"], archive, MAX_BINARY_BYTES, emit)

    emit(99, "Entpacke llama.cpp ...")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
    archive.unlink(missing_ok=True)

    binary = server_binary()
    if binary is None:
        raise RuntimeError("llama-server not found in archive")
    if platform.system() != "Windows":
        binary.chmod(0o755)
    emit(100, f"llama.cpp installiert: {binary}", state="done")
    return str(binary)


def _download_file(url: str, target: Path, max_bytes: int, emit: Any) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        if total > max_bytes:
            raise RuntimeError(f"Download zu groß ({total} Bytes): {url}")
        received = 0
        last_percent = -1
        with open(target, "wb") as fh:
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
            emit(0, f"Lade {entry['file']} ...")
            _download_file(entry["url"], partial, MAX_MODEL_BYTES, emit)
            partial.replace(target)
            emit(100, "fertig", state="done")
        except Exception as exc:
            logger.exception("LLM model download failed: %s", entry["name"])
            partial.unlink(missing_ok=True)
            emit(0, str(exc), state="error")
        finally:
            with _download_lock:
                _downloads_running.discard(entry["name"])

    threading.Thread(target=worker, daemon=True, name=f"llm-download-{entry['name']}").start()
    return True


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


def ensure_running() -> str:
    """Start llama-server if needed; returns the OpenAI-compatible base URL."""
    global _server_process, _server_model
    with _server_lock:
        base_url = f"http://127.0.0.1:{SERVER_PORT}/v1"
        if _server_process is not None and _server_process.poll() is None:
            return base_url

        binary = server_binary()
        if binary is None:
            raise RuntimeError("llama.cpp ist nicht installiert — in den Einstellungen einrichten")
        model_file = _pick_model_file()

        vram_mb, _ = _gpu_vram_mb()
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
            "999" if vram_mb else "0",
        ]
        logger.info("starting llama-server: %s", " ".join(cmd))
        hub.publish(
            "engine.status",
            {"engine": "llm", "state": "loading", "detail": model_file.name},
        )
        _server_process = subprocess.Popen(
            cmd,
            cwd=str(binary.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _server_model = model_file.stem

        deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if _server_process.poll() is not None:
                _server_process = None
                raise RuntimeError("llama-server wurde unerwartet beendet")
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
    hardware = probe_hardware()
    return {
        "binary_installed": server_binary() is not None,
        "server_running": _server_process is not None and _server_process.poll() is None,
        "active_model": _server_model,
        "hardware": hardware,
        "recommended": recommend_model(hardware),
        "catalog": MODEL_CATALOG,
        "installed": list_installed_models(),
        "models_dir": str(llm_models_dir()),
    }
