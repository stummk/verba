"""Process lifetime: desktop mode stops with its window, server mode does not.

Desktop mode is a local single-user app started by a double click, so it must
not linger in the background once the user is done with it. The UI holds a
WebSocket while it is open; when the last one is gone and none comes back
within a short grace period (a reload reconnects in about a second), the
process exits. Server mode keeps running until its service is stopped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading

from .events import hub

logger = logging.getLogger(__name__)

# grace period for reloads and short offline blips before the process exits
IDLE_EXIT_SECONDS = 6.0

_watchdog: asyncio.Task | None = None


def desktop_mode() -> bool:
    return os.environ.get("VERBA_DESKTOP_MODE") == "1"


def idle_exit_seconds() -> float:
    """Grace period; 0 (via VERBA_IDLE_EXIT_SECONDS) disables the watchdog."""
    raw = os.environ.get("VERBA_IDLE_EXIT_SECONDS")
    if raw is None:
        return IDLE_EXIT_SECONDS
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return IDLE_EXIT_SECONDS


def stop_process(delay: float = 0.1) -> None:
    """Ask the server to shut down (uvicorn handles SIGTERM gracefully)."""
    threading.Timer(delay, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


async def _exit_when_idle() -> None:
    await asyncio.sleep(idle_exit_seconds())
    if hub.client_count:
        return  # the UI came back (reload, brief disconnect)
    logger.info("desktop mode: no UI connected any more — shutting down")
    stop_process()


def arm_idle_watchdog() -> None:
    """Start the grace period after a UI disconnect (desktop mode only)."""
    global _watchdog
    if not desktop_mode() or not idle_exit_seconds():
        return
    if _watchdog and not _watchdog.done():
        _watchdog.cancel()
    _watchdog = asyncio.get_running_loop().create_task(_exit_when_idle())


def cancel_idle_watchdog() -> None:
    """A UI connected — cancel a pending shutdown."""
    global _watchdog
    if _watchdog and not _watchdog.done():
        _watchdog.cancel()
    _watchdog = None
