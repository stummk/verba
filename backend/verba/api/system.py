"""System status and first-run setup endpoints."""

from __future__ import annotations

import threading

from fastapi import APIRouter
from pydantic import BaseModel

from .. import __version__, config, datamove, lifecycle, setup_check
from ..services import updates
from .deps import AdminUser

router = APIRouter(prefix="/api/system", tags=["system"])


class SetupRunRequest(BaseModel):
    include_optional: bool = True


def _data_move_pending() -> bool:
    """Whether a chosen data directory is still waiting for a restart.

    A boolean, not the path: the dashboard only needs to know that it has to
    remind, and the location is an administrator's business.
    """
    settings = config.get_settings()
    return not datamove.same_path(config.configured_data_dir(settings), config.data_dir(settings))


def _status() -> dict:
    status = setup_check.system_status()
    status["version"] = __version__
    status["desktop_mode"] = lifecycle.desktop_mode()
    status["data_move_pending"] = _data_move_pending()
    # from the last check only — the status is read on every page load and
    # must never wait for GitHub
    status.update(updates.summary())
    return status


@router.get("/status")
def get_status() -> dict:
    return _status()


@router.get("/info")
def get_info(user: dict = AdminUser) -> dict:
    """General machine and app facts for the settings page."""
    info = setup_check.system_info()
    info["version"] = __version__
    return info


@router.post("/setup/run")
def run_setup(body: SetupRunRequest, user: dict = AdminUser) -> dict:
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


@router.post("/setup/complete")
def complete_setup(user: dict = AdminUser) -> dict:
    """Mark the first-run wizard as finished.

    The wizard walks through installation, workspace, Whisper, LLM and search;
    every step can be skipped with its defaults, so "finished" means the user
    reached the end — not that every component is installed. Skipping the
    wizard as a whole does not call this, so the reminder stays.
    """
    settings = config.get_settings()
    settings.setup.completed = True
    config.save_settings(settings)
    return _status()


@router.get("/update")
def get_update(refresh: bool = False, user: dict = AdminUser) -> dict:
    """Version in use, newest release, and the state of a running update.

    `refresh` asks GitHub again instead of using the cached answer — that is
    what the button next to the version does.
    """
    return updates.check(force=refresh)


@router.post("/update")
def start_update(user: dict = AdminUser) -> dict:
    """Download and install the newest release; progress arrives via WebSocket."""
    return updates.start_update()


@router.post("/shutdown")
def shutdown(user: dict = AdminUser) -> dict:
    """Stop the local desktop process after the response has been sent."""
    if not lifecycle.desktop_mode():
        return {"stopped": False}
    lifecycle.stop_process()
    return {"stopped": True}
