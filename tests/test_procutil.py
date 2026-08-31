"""Child processes must not flash a console window on Windows.

The packaged desktop build runs without a console, so every console program
started from it (nvidia-smi, ffmpeg, pip, llama-server) would open — and
immediately close — a window of its own on the user's desktop.
"""

from __future__ import annotations

import subprocess
import sys

from verba import procutil
from verba.services import audio, hardware, llamacpp


def test_no_window_flag_matches_the_platform():
    if sys.platform == "win32":
        assert procutil.NO_WINDOW == subprocess.CREATE_NO_WINDOW
    else:
        assert procutil.NO_WINDOW == 0  # Popen rejects a non-zero value here


def test_run_passes_the_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: seen.update(kwargs))
    procutil.run(["echo", "hi"], capture_output=True)
    assert seen["creationflags"] == procutil.NO_WINDOW
    assert seen["capture_output"] is True


def test_popen_passes_the_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: seen.update(kwargs))
    procutil.popen(["echo", "hi"], stdout=subprocess.DEVNULL)
    assert seen["creationflags"] == procutil.NO_WINDOW


def test_explicit_flags_win(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: seen.update(kwargs))
    procutil.run(["echo", "hi"], creationflags=0)
    assert seen["creationflags"] == 0


def test_spawning_services_go_through_procutil():
    """The call sites, not just the helper — a plain `subprocess.run` here is
    exactly the regression this module exists to prevent."""
    for module in (hardware, audio, llamacpp):
        assert module.procutil is procutil


def test_gpu_probe_is_hidden(monkeypatch):
    """nvidia-smi runs on every hardware probe — the most frequent spawn."""
    seen = {}

    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: seen.update(kwargs) or Result())
    hardware.gpu_info()
    assert seen["creationflags"] == procutil.NO_WINDOW
