"""Spawning helpers — one place that keeps Windows consoles off the screen.

A child console program (nvidia-smi, ffmpeg, pip, llama-server) opens its own
console window when the parent process has none, and the packaged desktop
build runs without one. Every spawn therefore goes through here, so no black
window flashes over the user's desktop while jobs run.
"""

from __future__ import annotations

import subprocess
from typing import Any

#: Windows-only flag; 0 elsewhere, so it can be passed unconditionally.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` that does not pop up a console window on Windows."""
    kwargs.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen` that does not pop up a console window on Windows."""
    kwargs.setdefault("creationflags", NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)
