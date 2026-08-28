"""Does this machine actually run that model? — memory checks for local engines.

Two questions, one module:

1. *Before* a download or a load: is this system suitable for this Whisper
   model, for this GGUF? The answer is a verdict (`ok` / `tight` / `no`) with a
   German sentence the UI puts next to the model. Only local engines are rated:
   an OpenAI-compatible endpoint runs on someone else's hardware, so nothing is
   said about it.
2. *During* a load or a run: memory really did run out. The verdict decides
   whether the GPU is skipped in the first place, and `is_oom()` turns a
   backend allocation failure into a German message plus a CPU retry — the
   process reports instead of dying.

Only the standard library is used here (`setup_check` imports this during
bootstrap), and the probe is cached for a moment so one settings page does not
run `nvidia-smi` three times.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)

# ── verdict levels ────────────────────────────────────────────────────

OK = "ok"  # runs with headroom
TIGHT = "tight"  # runs, with a caveat: memory nearly full, or GPU→CPU
NO = "no"  # does not fit — the UI warns before the download
UNKNOWN = "unknown"  # no usable probe (unusual OS, container without /proc)

#: Everything above this share of the total counts as "tight" even when free.
TIGHT_SHARE = 0.8
#: The OS and Verba itself keep running next to a CPU-loaded model.
CPU_RESERVE_MB = 1024
#: Loading into VRAM needs a little more than the weights (context, workspace).
VRAM_MARGIN_MB = 512

PROBE_TTL_S = 3.0


class InsufficientMemory(RuntimeError):
    """Raised instead of attempting a load that cannot succeed.

    The message is German: it reaches the user through a job error and through
    HTTPException details.
    """


# ── the probe ─────────────────────────────────────────────────────────


def ram_mb() -> tuple[int, int]:
    """(total, available) in MB — 0/0 when the platform cannot be read."""
    try:
        if platform.system() == "Windows":

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
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        total = re.search(r"MemTotal:\s+(\d+) kB", meminfo)
        available = re.search(r"MemAvailable:\s+(\d+) kB", meminfo)
        return (
            int(total.group(1)) // 1024 if total else 0,
            int(available.group(1)) // 1024 if available else 0,
        )
    except (OSError, AttributeError, ValueError):
        return 0, 0


def gpu_info() -> dict[str, Any]:
    """Name plus total/free VRAM in MB via nvidia-smi; zeros without a GPU."""
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


_probe_lock = threading.Lock()
_probe_cache: tuple[float, dict[str, Any]] | None = None


def probe(*, fresh: bool = False) -> dict[str, Any]:
    """RAM and VRAM of this machine. Cached for `PROBE_TTL_S` seconds."""
    global _probe_cache
    with _probe_lock:
        now = time.monotonic()
        if not fresh and _probe_cache is not None and now - _probe_cache[0] < PROBE_TTL_S:
            return dict(_probe_cache[1])
        ram_total, ram_available = ram_mb()
        gpu = gpu_info()
        result = {
            "ram_total_mb": ram_total,
            "ram_available_mb": ram_available,
            "gpu_name": gpu["name"],
            "vram_total_mb": gpu["vram_total_mb"],
            "vram_free_mb": gpu["vram_free_mb"],
        }
        _probe_cache = (now, result)
        return dict(result)


def invalidate_probe() -> None:
    """Drop the cached probe (after a model was unloaded, and in tests)."""
    global _probe_cache
    with _probe_lock:
        _probe_cache = None


def has_gpu(hw: dict[str, Any] | None = None) -> bool:
    return (hw or probe())["vram_total_mb"] > 0


# ── requirements ──────────────────────────────────────────────────────

#: Memory a Whisper model needs while running, including beam search and
#: activations — not just the weights on disk. CPU runs int8, GPU float16.
WHISPER_REQUIREMENTS: dict[str, dict[str, int]] = {
    "tiny": {"cpu_mb": 400, "gpu_mb": 900},
    "base": {"cpu_mb": 550, "gpu_mb": 1100},
    "small": {"cpu_mb": 1000, "gpu_mb": 1800},
    "medium": {"cpu_mb": 2200, "gpu_mb": 3800},
    "large-v2": {"cpu_mb": 3600, "gpu_mb": 6200},
    "large-v3": {"cpu_mb": 3600, "gpu_mb": 6200},
    "large-v3-turbo": {"cpu_mb": 1900, "gpu_mb": 3200},
    "distil-large-v3": {"cpu_mb": 2100, "gpu_mb": 3600},
}
#: A model nobody knows (custom folder without files on disk yet) is assumed
#: to be a large one, so the warning errs on the safe side.
WHISPER_FALLBACK = {"cpu_mb": 3600, "gpu_mb": 6200}

#: Quality ladder for the recommendation: every step is clearly better than
#: the one before, so the last one that fits is the one to suggest. The other
#: built-ins stay selectable, they are just never *suggested*.
SUGGESTION_ORDER = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]


def whisper_requirement(name: str, models_dir: Path | None = None) -> dict[str, int]:
    """Requirement for a built-in name, or derived from the folder on disk."""
    if name in WHISPER_REQUIREMENTS:
        return dict(WHISPER_REQUIREMENTS[name])
    if models_dir is not None:
        candidate = Path(name) if Path(name).is_absolute() else models_dir / name
        model_bin = candidate / "model.bin"
        try:
            weights_mb = model_bin.stat().st_size // (1024 * 1024)
        except OSError:
            weights_mb = 0
        if weights_mb:
            # runtime overhead scales with the model: measured between 1.3×
            # (large) and 2× (tiny), the constant term covers the rest
            return {
                "cpu_mb": int(weights_mb * 1.4) + 300,
                "gpu_mb": int(weights_mb * 1.8) + 600,
            }
    return dict(WHISPER_FALLBACK)


def gguf_requirement(size_mb: int) -> int:
    """Memory a GGUF of that size needs: weights plus the 16k context."""
    return int(size_mb * 1.1) + 1200


# ── rating ────────────────────────────────────────────────────────────


def _gb(mb: int | float) -> str:
    return f"{mb / 1024:.1f} GB".replace(".", ",")


def rate(needs_mb: int, *, total_mb: int, free_mb: int, device: str) -> dict[str, Any]:
    """One verdict: does `needs_mb` fit into that memory right now?

    `device` is "gpu" or "cpu" and only shapes the wording.
    """
    memory = "VRAM" if device == "gpu" else "RAM"
    base = {"device": device, "needs_mb": needs_mb, "total_mb": total_mb, "free_mb": free_mb}
    if total_mb <= 0:
        return {
            **base,
            "level": UNKNOWN,
            "message": f"Speicher konnte nicht ermittelt werden — Bedarf ca. {_gb(needs_mb)}.",
        }
    if needs_mb > total_mb:
        return {
            **base,
            "level": NO,
            "message": (
                f"Zu groß für dieses System: braucht ca. {_gb(needs_mb)}, "
                f"vorhanden sind {_gb(total_mb)} {memory}."
            ),
        }
    if needs_mb > free_mb:
        return {
            **base,
            "level": TIGHT,
            "message": (
                f"Passt knapp: braucht ca. {_gb(needs_mb)}, frei sind aktuell nur "
                f"{_gb(free_mb)} von {_gb(total_mb)} {memory}. Andere Programme schließen."
            ),
        }
    if needs_mb > total_mb * TIGHT_SHARE:
        return {
            **base,
            "level": TIGHT,
            "message": (f"Passt knapp: braucht ca. {_gb(needs_mb)} von {_gb(total_mb)} {memory}."),
        }
    return {
        **base,
        "level": OK,
        "message": (f"Geeignet: braucht ca. {_gb(needs_mb)}, frei sind {_gb(free_mb)} {memory}."),
    }


def _cpu_budget(hw: dict[str, Any]) -> tuple[int, int]:
    total = max(0, hw["ram_total_mb"] - CPU_RESERVE_MB)
    free = max(0, hw["ram_available_mb"] - CPU_RESERVE_MB)
    return total, free


def _gpu_budget(hw: dict[str, Any]) -> tuple[int, int]:
    return hw["vram_total_mb"], hw["vram_free_mb"]


def capacity(hw: dict[str, Any]) -> dict[str, Any]:
    """The same machine, seen as "nothing else running".

    A *verdict* asks whether the model loads right now, so it counts free
    memory. A *recommendation* is a lasting choice — it must not change every
    time a browser window opens, so it counts what the machine has.
    """
    return {
        **hw,
        "ram_available_mb": hw["ram_total_mb"],
        "vram_free_mb": hw["vram_total_mb"],
    }


# ── Whisper ───────────────────────────────────────────────────────────


def _rate_whisper(
    name: str, device: str, hw: dict[str, Any], models_dir: Path | None
) -> dict[str, Any]:
    """The verdict itself — no recommendation appended (that would recurse)."""
    need = whisper_requirement(name, models_dir)
    if device in ("auto", "cuda") and has_gpu(hw):
        total, free = _gpu_budget(hw)
        verdict = rate(need["gpu_mb"] + VRAM_MARGIN_MB, total_mb=total, free_mb=free, device="gpu")
        if verdict["level"] != NO:
            return verdict
        cpu_total, cpu_free = _cpu_budget(hw)
        on_cpu = rate(need["cpu_mb"], total_mb=cpu_total, free_mb=cpu_free, device="cpu")
        if on_cpu["level"] in (OK, TIGHT):
            return {
                **on_cpu,
                "level": TIGHT,
                "message": (
                    f"Zu groß für den Grafikspeicher ({_gb(total)} VRAM) — läuft auf der CPU, "
                    f"dafür deutlich langsamer."
                ),
            }
        return on_cpu
    cpu_total, cpu_free = _cpu_budget(hw)
    return rate(need["cpu_mb"], total_mb=cpu_total, free_mb=cpu_free, device="cpu")


def check_whisper_model(
    name: str,
    *,
    device: str = "auto",
    hw: dict[str, Any] | None = None,
    models_dir: Path | None = None,
) -> dict[str, Any]:
    """Verdict for one Whisper model, including the GPU→CPU fallback.

    A model too large for the VRAM is not a "no" as long as the CPU can carry
    it — that is what the service does at load time, so the verdict says so
    instead of pretending the model is unusable. A real "no" carries the
    recommendation for this machine, because that is the moment it helps.
    """
    hw = hw or probe()
    verdict = _rate_whisper(name, device, hw, models_dir)
    if verdict["level"] == NO:
        fitting = suggest_whisper_model(hw, device=device)
        if fitting:
            verdict["message"] += f" Empfehlung für dieses System: {fitting}."
    return {"name": name, **verdict}


def suggest_whisper_model(hw: dict[str, Any] | None = None, *, device: str = "auto") -> str:
    """Best built-in model this machine still runs comfortably.

    Walked from small to large along the quality ladder, so the answer is the
    last one rated `ok`; only if nothing has headroom does a `tight` model win
    (better a slow recommendation than none).
    """
    hw = capacity(hw or probe())
    comfortable = ""
    tight = ""
    for name in SUGGESTION_ORDER:
        level = _rate_whisper(name, device, hw, None)["level"]
        if level == OK:
            comfortable = name
        elif level == TIGHT:
            tight = name
    return comfortable or tight


def whisper_fit(names: list[str], *, hw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verdicts for a list of model names, plus the probe they were rated on."""
    settings = config.get_settings()
    hw = hw or probe()
    models_dir = config.models_dir(settings)
    device = settings.whisper.device
    return {
        "hardware": hw,
        "device": device,
        "suggested": suggest_whisper_model(hw, device=device),
        "models": {
            name: check_whisper_model(name, device=device, hw=hw, models_dir=models_dir)
            for name in names
        },
    }


def ensure_whisper_fits(name: str, *, device: str = "auto") -> dict[str, Any]:
    """Called right before loading: refuse a load that cannot succeed.

    Returns the verdict so the caller can skip the GPU attempt; raises
    `InsufficientMemory` when neither device has the room.
    """
    verdict = check_whisper_model(
        name, device=device, models_dir=config.models_dir(config.get_settings())
    )
    if verdict["level"] == NO:
        raise InsufficientMemory(
            f"Modell '{name}' passt nicht in den Speicher. {verdict['message']}"
        )
    return verdict


# ── local LLM (llama.cpp) ─────────────────────────────────────────────


def check_llm_model(needs_mb: int, *, hw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verdict for a GGUF of that memory need, GPU first, CPU as the fallback."""
    hw = hw or probe()
    if has_gpu(hw):
        total, free = _gpu_budget(hw)
        verdict = rate(needs_mb, total_mb=total, free_mb=free, device="gpu")
        if verdict["level"] != NO:
            return verdict
        cpu_total, cpu_free = _cpu_budget(hw)
        on_cpu = rate(needs_mb, total_mb=cpu_total, free_mb=cpu_free, device="cpu")
        if on_cpu["level"] in (OK, TIGHT):
            return {
                **on_cpu,
                "level": TIGHT,
                "message": (
                    f"Zu groß für den Grafikspeicher ({_gb(total)} VRAM) — läuft im "
                    f"Arbeitsspeicher, dafür deutlich langsamer."
                ),
            }
        return on_cpu
    cpu_total, cpu_free = _cpu_budget(hw)
    return rate(needs_mb, total_mb=cpu_total, free_mb=cpu_free, device="cpu")


def model_budget(hw: dict[str, Any] | None = None) -> dict[str, Any]:
    """How much memory a model could realistically occupy here, per device.

    Used where Verba does *not* control the model: an OpenAI-compatible
    endpoint on localhost runs on this machine, but which model it serves and
    whether it offloads to the GPU is the other program's business. So the
    numbers are handed out and the UI phrases them as an estimate.
    """
    hw = hw or probe()
    cpu_total, cpu_free = _cpu_budget(hw)
    return {
        "cpu_mb": cpu_free,
        "cpu_total_mb": cpu_total,
        "gpu_mb": max(0, hw["vram_free_mb"] - VRAM_MARGIN_MB) if has_gpu(hw) else 0,
        "gpu_total_mb": hw["vram_total_mb"],
    }


# ── embedding models (search index) ───────────────────────────────────


def embedding_requirement(size_mb: int) -> int:
    """Memory a sentence-transformers model needs while encoding.

    Float32 weights plus the torch runtime and one batch of activations; the
    search always encodes on the CPU (see vectorstore), so there is no GPU
    variant to rate.
    """
    return int(size_mb * 1.5) + 700


def check_embedding_model(
    size_mb: int,
    *,
    hw: dict[str, Any] | None = None,
    alternatives: list[tuple[str, int]] | None = None,
) -> dict[str, Any]:
    """Verdict for one embedding model — RAM only, the index runs on the CPU.

    `alternatives` are (label, size_mb) pairs from the catalog: when the model
    does not fit, the message names the largest one that does.
    """
    hw = hw or probe()
    cpu_total, cpu_free = _cpu_budget(hw)
    verdict = rate(
        embedding_requirement(size_mb), total_mb=cpu_total, free_mb=cpu_free, device="cpu"
    )
    if verdict["level"] == NO and alternatives:
        fitting = suggest_embedding_model(alternatives, hw)
        if fitting:
            verdict["message"] += f" Empfehlung für dieses System: {fitting}."
    return verdict


def suggest_embedding_model(models: list[tuple[str, int]], hw: dict[str, Any] | None = None) -> str:
    """Largest of the given (name, size_mb) pairs this machine can carry."""
    hw = capacity(hw or probe())
    comfortable = ""
    tight = ""
    for name, size_mb in sorted(models, key=lambda pair: pair[1]):
        level = check_embedding_model(size_mb, hw=hw)["level"]
        if level == OK:
            comfortable = name
        elif level == TIGHT:
            tight = name
    return comfortable or tight


def offload_to_gpu(needs_mb: int, hw: dict[str, Any] | None = None) -> bool:
    """Whether llama.cpp should put the layers on the GPU.

    Offloading a model that does not fit is what makes llama-server die on
    startup, so a model larger than the free VRAM stays in RAM.
    """
    hw = hw or probe()
    if not has_gpu(hw):
        return False
    return needs_mb + VRAM_MARGIN_MB <= hw["vram_free_mb"]


# ── running out of memory ─────────────────────────────────────────────

_OOM_MARKERS = (
    "out of memory",
    "outofmemory",
    "out_of_memory",
    "alloc_failed",
    "failed to allocate",
    "cannot allocate",
    "unable to allocate",
    "bad_alloc",
    "not enough memory",
    "insufficient memory",
    "insufficient system resources",
    "paging file is too small",
    "cudamalloc",
    "killed",  # the Linux OOM killer took llama-server
)


def is_oom(error: BaseException | str) -> bool:
    """True when this failure is a memory shortage rather than a bug.

    Whisper (CTranslate2), llama.cpp and Python itself all phrase it
    differently, so the check is textual — plus `MemoryError` itself.
    """
    if isinstance(error, MemoryError):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _OOM_MARKERS)


def oom_message(device: str, *, name: str = "", hw: dict[str, Any] | None = None) -> str:
    """German explanation for an allocation failure, with the current numbers."""
    hw = hw or probe(fresh=True)
    subject = f"Modell '{name}'" if name else "Das Modell"
    if device == "gpu":
        return (
            f"{subject}: Der Grafikspeicher ist voll "
            f"({_gb(hw['vram_free_mb'])} von {_gb(hw['vram_total_mb'])} VRAM frei)."
        )
    return (
        f"{subject}: Der Arbeitsspeicher ist voll "
        f"({_gb(hw['ram_available_mb'])} von {_gb(hw['ram_total_mb'])} RAM frei)."
    )
