"""What the machine is doing *right now* — the live bars in the settings.

`hardware.py` answers a lasting question ("does this model fit?") and caches
its probe for ten seconds accordingly. This module answers a different one:
how busy are processor, memory and graphics card at this second. Same
measurements, a much shorter shelf life, and one number `hardware` never
needs — the processor load, which is not a level but a difference between two
readings.

Only the standard library is used (no psutil): the counters come from
`GetSystemTimes` on Windows and `/proc/stat` on Linux, the graphics card from
the same `nvidia-smi` call `hardware` uses.

The settings page polls this every couple of seconds, and several open tabs
must not multiply that into a stream of `nvidia-smi` spawns. So a reading is
cached for `TTL_S` and, once it ages, renewed in a background thread while
the asking request is served the value from a moment ago: a bar that lags one
interval is worth more than a request that waits for a process spawn.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import threading
import time
from pathlib import Path
from typing import Any

from . import hardware

logger = logging.getLogger(__name__)

#: How long a reading stays fresh — the UI polls at about this rate.
TTL_S = 2.0
#: The very first reading has no predecessor to subtract; rather than an empty
#: bar it measures this short window itself. Only ever paid once.
FIRST_WINDOW_S = 0.12


# ── processor ─────────────────────────────────────────────────────────


def _windows_cpu_times() -> tuple[float, float] | None:
    """(busy, total) from `GetSystemTimes`; kernel time includes idle."""

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def value(ft: FileTime) -> float:
        return float((ft.high << 32) | ft.low)

    idle, kernel, user = FileTime(), FileTime(), FileTime()
    ok = ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    )
    if not ok:
        return None
    total = value(kernel) + value(user)
    return total - value(idle), total


def _proc_cpu_times() -> tuple[float, float] | None:
    """(busy, total) from the aggregate `cpu` line of /proc/stat."""
    line = Path("/proc/stat").read_text(encoding="utf-8").split("\n", 1)[0]
    fields = [float(field) for field in line.split()[1:]]
    if len(fields) < 4:
        return None
    # user nice system idle iowait … — waiting for a disk is not work done
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
    total = sum(fields)
    return total - idle, total


def _cpu_times() -> tuple[float, float] | None:
    """The machine's busy/total counters, or None where none can be read."""
    try:
        if platform.system() == "Windows":
            return _windows_cpu_times()
        return _proc_cpu_times()
    except (OSError, AttributeError, ValueError):
        return None


def _loadavg_percent() -> float | None:
    """Fallback for a platform without either counter (macOS).

    Not the same measurement — the load average counts waiting processes and
    reacts over a minute — but it is a truthful picture of how busy the
    machine is, and the alternative is a bar that never moves.
    """
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        return None
    return round(min(100.0, load / (os.cpu_count() or 1) * 100), 1)


_last_cpu: tuple[float, float] | None = None


def cpu_percent() -> float | None:
    """Share of all cores that was busy since the previous reading, 0–100."""
    global _last_cpu
    previous, current = _last_cpu, _cpu_times()
    if current is None:
        return _loadavg_percent()
    if previous is None:
        time.sleep(FIRST_WINDOW_S)
        previous, current = current, _cpu_times()
        if current is None:
            return _loadavg_percent()
    _last_cpu = current
    busy, total = current[0] - previous[0], current[1] - previous[1]
    if total <= 0:  # two readings within the same clock tick
        return None
    return round(max(0.0, min(100.0, busy / total * 100)), 1)


def reset_cpu_baseline() -> None:
    """Forget the previous reading (tests, and after a long idle period)."""
    global _last_cpu
    _last_cpu = None


# ── the reading ───────────────────────────────────────────────────────


def _measure() -> dict[str, Any]:
    """One complete reading — no lock held while nvidia-smi runs."""
    ram_total, ram_available = hardware.ram_mb()
    gpu = hardware.gpu_info()
    return {
        "cpu_percent": cpu_percent(),
        "cpu_cores": os.cpu_count() or 0,
        "ram_total_mb": ram_total,
        "ram_used_mb": max(0, ram_total - ram_available),
        "gpu_name": gpu["name"],
        "gpu_percent": gpu["util_percent"],
        "vram_total_mb": gpu["vram_total_mb"],
        "vram_used_mb": max(0, gpu["vram_total_mb"] - gpu["vram_free_mb"]),
    }


_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None
_refreshing = False


def sample(*, fresh: bool = False) -> dict[str, Any]:
    """The current load of this machine, at most `TTL_S` old.

    Only the first caller ever waits for a measurement; an aged reading is
    handed out as it is and renewed in the background.
    """
    global _cache
    with _lock:
        cached = _cache
    if fresh or cached is None:
        result = _measure()
        with _lock:
            _cache = (time.monotonic(), result)
        return dict(result)
    if time.monotonic() - cached[0] >= TTL_S:
        _refresh_in_background()
    return dict(cached[1])


def _refresh_in_background() -> None:
    """Renew the cached reading without anybody waiting for it (one at a time)."""
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True

    def run() -> None:
        global _cache, _refreshing
        try:
            result = _measure()
            with _lock:
                _cache = (time.monotonic(), result)
        except Exception:  # a monitor must never take the process down
            logger.debug("background resource reading failed", exc_info=True)
        finally:
            with _lock:
                _refreshing = False

    threading.Thread(target=run, daemon=True, name="resource-monitor").start()


def invalidate() -> None:
    """Drop the cached reading (tests)."""
    global _cache
    with _lock:
        _cache = None
