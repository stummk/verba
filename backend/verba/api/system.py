"""System status and first-run setup endpoints."""

from __future__ import annotations

import threading

from fastapi import APIRouter
from pydantic import BaseModel

from .. import __version__, setup_check

router = APIRouter(prefix="/api/system", tags=["system"])


class SetupRunRequest(BaseModel):
    include_optional: bool = True


@router.get("/status")
def get_status() -> dict:
    status = setup_check.system_status()
    status["version"] = __version__
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
