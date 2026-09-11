"""Child processes must not flash a console window on Windows.

The packaged desktop build runs without a console, so every console program
started from it (nvidia-smi, ffmpeg, pip, llama-server) would open — and
immediately close — a window of its own on the user's desktop.

And a child that holds a model must not outlive the process that started it,
which on Windows is regularly not asked to stop but terminated outright.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from verba import procutil
from verba.services import audio, hardware, llamacpp

REPO_BACKEND = str(Path(__file__).resolve().parent.parent / "backend")

# spawns a sleeper the way Verba spawns llama-server, prints its pid and waits
_PARENT_CODE = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from verba import procutil\n"
    "child = procutil.popen("
    "[sys.executable, '-c', 'import time; time.sleep(120)'], kill_with_parent=True)\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(120)\n"
)


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


def test_the_tie_is_not_passed_on_to_popen(monkeypatch):
    """`kill_with_parent` is ours — Popen would reject it."""
    seen = {}

    class Fake:
        pid = 0

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: seen.update(kwargs) or Fake())
    monkeypatch.setattr(procutil, "_tie_to_this_process", lambda process: seen.update(tied=True))
    procutil.popen(["echo", "hi"], kill_with_parent=True)
    assert "kill_with_parent" not in seen
    assert seen["tied"] is True


def test_a_child_that_must_not_outlive_us_survives_nothing(tmp_path):
    """The real thing: a process is terminated, its tied child goes with it.

    Windows only — that is where a process is killed rather than signalled,
    and where no cgroup cleans up behind it. Elsewhere the graceful shutdown
    and the leftover check at the next start do the work.
    """
    if os.name != "nt":
        pytest.skip("the job object is the Windows answer")
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT_CODE, REPO_BACKEND],
        stdout=subprocess.PIPE,
        text=True,
    )
    child_pid = 0
    try:
        child_pid = int(parent.stdout.readline().strip())
        assert procutil.is_running(child_pid, Path(sys.executable).name)
        parent.kill()  # TerminateProcess: no cleanup runs anywhere
        parent.wait(timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not procutil.is_running(child_pid, Path(sys.executable).name):
                break
            time.sleep(0.25)
        assert not procutil.is_running(child_pid, Path(sys.executable).name)
    finally:
        if parent.poll() is None:
            parent.kill()
        if child_pid:
            procutil.terminate(child_pid)


def test_a_pid_is_only_ever_acted_on_together_with_its_name():
    """Pids are handed out again — the name is what keeps a leftover check
    from terminating whatever inherited the number."""
    sleeper = procutil.popen([sys.executable, "-c", "import time; time.sleep(60)"])
    name = ""
    try:
        # the name as the platform reports it — an image name on Windows, the
        # comm of a /proc entry on Linux
        name = procutil.process_name(sleeper.pid) or ""
        assert name.lower().startswith("python")
        assert procutil.is_running(sleeper.pid, name)
        assert procutil.is_running(sleeper.pid, name.upper())  # the name is not case work
        assert not procutil.is_running(sleeper.pid, "llama-server.exe")
        assert not procutil.is_running(0, name)
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)
    assert not procutil.is_running(sleeper.pid, name)


def test_terminate_ends_a_process_that_is_not_ours():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert procutil.terminate(sleeper.pid) is True
        sleeper.wait(timeout=30)  # gone — a wait that runs out fails the test
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


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
