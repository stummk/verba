"""Spawning helpers — one place that keeps Windows consoles off the screen.

A child console program (nvidia-smi, ffmpeg, pip, llama-server) opens its own
console window when the parent process has none, and the packaged desktop
build runs without one. Every spawn therefore goes through here, so no black
window flashes over the user's desktop while jobs run.

It is also where a child is tied to this process' lifetime
(`kill_with_parent=True`). Verba asks its long-lived children to stop when it
shuts down, but it is not always asked itself: on Windows a process is
usually not signalled but terminated outright — the Task Manager, the
installer of an update, `os.kill`, which is TerminateProcess there. A child
survives that, and llama-server or a running compiler then holds gigabytes of
memory that nobody owns any more. A job object takes them along.

Only children that must not outlive Verba ask for this. The installer of an
update and the relaunch into a new version are spawned *because* they have to
survive it.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Windows-only flag; 0 elsewhere, so it can be passed unconditionally.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_JOB_KILL_ON_CLOSE = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
_JOB_EXTENDED_LIMITS = 9  # JobObjectExtendedLimitInformation
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_job_lock = threading.Lock()
_job_handle: int | None = None
_job_asked = False


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` that does not pop up a console window on Windows."""
    kwargs.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def popen(cmd: list[str], *, kill_with_parent: bool = False, **kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen` that does not pop up a console window on Windows.

    `kill_with_parent` ends the child together with this process, however
    this process ends — see the module docstring.
    """
    kwargs.setdefault("creationflags", NO_WINDOW)
    process = subprocess.Popen(cmd, **kwargs)
    if kill_with_parent:
        _tie_to_this_process(process)
    return process


# ── a child that must not outlive us ──────────────────────────────────


def _tie_to_this_process(process: subprocess.Popen) -> None:
    """Put the child into this process' job object (Windows only).

    Elsewhere there is nothing this cheap and this reliable: the POSIX answer
    (`PR_SET_PDEATHSIG`) hangs off the *thread* that spawned, and here that is
    a job worker or a request thread, either of which may end long before the
    child should. A service started by systemd is covered by its cgroup, and
    for everything else there is the leftover check at the next start
    (`services/llamacpp.reap_stale_server`).
    """
    if os.name != "nt":
        return
    job = _kill_on_close_job()
    if job is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    child = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid)
    if not child:
        logger.warning("could not open child %d to tie it to this process", process.pid)
        return
    try:
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not kernel32.AssignProcessToJobObject(job, child):
            logger.warning(
                "child %d could not be assigned to the job object (error %d)",
                process.pid,
                ctypes.get_last_error(),
            )
    finally:
        kernel32.CloseHandle(child)


def _kill_on_close_job() -> int | None:
    """This process' job object, created once; None where there is none."""
    global _job_handle, _job_asked
    with _job_lock:
        if not _job_asked:
            _job_asked = True
            _job_handle = _create_kill_on_close_job()
        return _job_handle


def _create_kill_on_close_job() -> int | None:
    """A job object that kills its members once its last handle is gone.

    That handle is ours and nobody else's, so Windows closes it whichever way
    this process ends — terminated included — and takes the children with it.
    The structures are declared here rather than at module level: `wintypes`
    does not import on anything but Windows.
    """
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ulong_ptr),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        logger.warning("no job object: children may outlive this process")
        return None
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    if not kernel32.SetInformationJobObject(
        job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        logger.warning(
            "job object refused the kill-on-close limit (error %d)", ctypes.get_last_error()
        )
        kernel32.CloseHandle(job)
        return None
    return int(job)


# ── a process somebody else left behind ───────────────────────────────


def is_running(pid: int, name: str) -> bool:
    """True when `pid` is alive *and* is a process called `name`.

    Both halves matter: a pid noted down before a crash may since have been
    handed to something entirely unrelated, and that must never be the thing
    a leftover check terminates.
    """
    if pid <= 0:
        return False
    running = process_name(pid)
    if running is None:
        return False
    return running.lower() == name.lower()


def process_name(pid: int) -> str | None:
    """The executable name behind a pid, None when it cannot be established.

    Unknown is not the same as gone: a process this cannot identify is left
    alone rather than treated as free to kill.
    """
    if os.name == "nt":
        return _windows_process_name(pid)
    comm = Path(f"/proc/{pid}/comm")
    try:
        return comm.read_text(encoding="utf-8", errors="replace").strip() or None
    except FileNotFoundError:
        if Path("/proc").is_dir():
            return None  # a /proc that answers and has no such pid: it is gone
    except OSError:
        return None
    try:  # no /proc (macOS, BSD): ask ps
        result = run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (result.stdout or "").strip()
    return Path(line).name if line else None


def _windows_process_name(pid: int) -> str | None:
    try:
        result = run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            # tasklist answers in the console codepage, and its "no such task"
            # sentence is translated — a German one does not decode as cp1252
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (result.stdout or "").strip()
    # no match is not an error for tasklist: it says so in a sentence
    if not line.startswith('"'):
        return None
    return line.split('","')[0].strip('"') or None


def terminate(pid: int) -> bool:
    """End a process that is not a child of ours; True when the signal went.

    On Windows `os.kill` is TerminateProcess — which is what is wanted here:
    the process is nobody's any more, and there is no one left to ask it
    politely. On POSIX SIGTERM still gives it its own shutdown.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError) as exc:
        logger.warning("could not stop the leftover process %d: %s", pid, exc)
        return False
    return True
