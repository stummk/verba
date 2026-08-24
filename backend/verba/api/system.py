"""System status and first-run setup endpoints."""

from __future__ import annotations

import os
import signal
import threading

from fastapi import APIRouter
from pydantic import BaseModel

from .. import __version__, setup_check

router = APIRouter(prefix="/api/system", tags=["system"])


class SetupRunRequest(BaseModel):
    include_optional: bool = True


def _stop_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


@router.get("/status")
def get_status() -> dict:
    status = setup_check.system_status()
    status["version"] = __version__
    status["desktop_mode"] = os.environ.get("VERBA_DESKTOP_MODE") == "1"
    return status


@router.get("/info")
def get_info() -> dict:
    """General machine and app facts for the settings page."""
    info = setup_check.system_info()
    info["version"] = __version__
    return info


@router.post("/setup/run")
def run_setup(body: SetupRunRequest) -> dict:
    """Start the setup in a background thread; progress arrives via WebSocket."""
    if setup_check.progress.running:
        return {"started": False, "reason": "Setup is already running."}
    thread = threading.Thread(
        target=setup_check.run_setup,
        kwargs={"include_optional": body.include_optional},
        daemon=True,
        name="setup-runner",
    )
    thread.start()
    return {"started": True}


@router.post("/shutdown")
def shutdown() -> dict:
    """Stop the local desktop process after the response has been sent."""
    if os.environ.get("VERBA_DESKTOP_MODE") != "1":
        return {"stopped": False}
    threading.Timer(0.1, _stop_process).start()
    return {"stopped": True}
