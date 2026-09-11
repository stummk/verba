"""Process lifetime: desktop mode stops with its window, server mode does not.

Desktop mode is a local single-user app started by a double click, so it must
not linger in the background once the user is done with it. The UI holds a
WebSocket while it is open; when the last one is gone and none comes back
within a short grace period (a reload reconnects in about a second), the
process exits. Server mode keeps running until its service is stopped.

Ending has to be orderly, because a desktop machine is the user's own: the
models a transcription and a local LLM keep in RAM and VRAM are gigabytes, and
they are given back here (`release_local_models`), not left to the exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from typing import Any

from .events import hub

logger = logging.getLogger(__name__)

# grace period for reloads and short offline blips before the process exits
IDLE_EXIT_SECONDS = 6.0

_watchdog: asyncio.Task | None = None
_server: Any = None  # the uvicorn Server, once run.py handed it over
_exiting = False


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


def bind_server(server: Any) -> None:
    """run.py hands the uvicorn server over so the exit can be asked for.

    Without it there is only `os.kill(SIGTERM)`, and on Windows that is no
    signal at all: `os.kill` maps everything but the two console events onto
    TerminateProcess, so the process dies on the spot — the shutdown half of
    the lifespan never runs, and the models stay in memory. A llama-server
    child even outlives its parent there and keeps its GGUF resident.
    """
    global _server
    _server = server


def _request_exit() -> None:
    """Ask uvicorn to stop; it polls the flag and then runs the lifespan."""
    if _server is not None:
        _server.should_exit = True
        return
    # no server bound (tests, a foreign ASGI host): the signal is all there is
    os.kill(os.getpid(), signal.SIGTERM)


def stop_process(delay: float = 0.1) -> None:
    """Ask the server to shut down once the current response has gone out."""
    global _exiting
    _exiting = True
    threading.Timer(delay, _request_exit).start()


def release_local_models() -> None:
    """Give back what the local engines hold before the process ends.

    Only the exit of *this* process frees its own memory automatically, and
    that is not where all of it sits: llama-server is a child that survives
    its parent, and a Whisper model on the GPU releases the VRAM in its
    destructor. Desktop mode ends every time the window is closed, so this
    has to leave the machine free rather than a model resident on it.

    Each engine is released on its own — a failure of one must not keep the
    memory of the next.
    """
    from .services import llamacpp, vectorstore, whisper

    for engine, release in (
        ("llama-server", llamacpp.stop_server),
        ("whisper", whisper.unload_model),
        ("embeddings", vectorstore.unload_model),
    ):
        try:
            release()
        except Exception:
            logger.exception("%s could not be released at shutdown", engine)


async def _exit_when_idle() -> None:
    await asyncio.sleep(idle_exit_seconds())
    if hub.client_count:
        return  # the UI came back (reload, brief disconnect)
    logger.info("desktop mode: no UI connected any more — shutting down")
    stop_process()


def arm_idle_watchdog() -> None:
    """Start the grace period after a UI disconnect (desktop mode only)."""
    global _watchdog
    if _exiting or not desktop_mode() or not idle_exit_seconds():
        return  # already on the way out: the closing UI sockets change nothing
    if _watchdog and not _watchdog.done():
        _watchdog.cancel()
    _watchdog = asyncio.get_running_loop().create_task(_exit_when_idle())


def cancel_idle_watchdog() -> None:
    """A UI connected — cancel a pending shutdown."""
    global _watchdog
    if _watchdog and not _watchdog.done():
        _watchdog.cancel()
    _watchdog = None
