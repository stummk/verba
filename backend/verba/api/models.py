"""Model management: Whisper models and local LLM (llama.cpp + GGUF)."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import diarize, llamacpp, whisper
from .deps import AdminUser

router = APIRouter(prefix="/api/models", tags=["models"])

# built-in size names or HuggingFace repo ids (org/name); segments must start
# with a word character so dot-prefixed path tricks ("../x") are rejected
_NAME_RE = re.compile(r"^\w[\w.-]*(/\w[\w.-]*)?$")


class DownloadRequest(BaseModel):
    name: str


class SetupRequest(BaseModel):
    #: install again over an existing installation (to reach the GPU)
    force: bool = False


@router.get("")
def list_models() -> dict:
    return whisper.list_models()


@router.post("/download", status_code=202)
def download_model(body: DownloadRequest, user: dict = AdminUser) -> dict:
    name = body.name.strip()
    if not name or not _NAME_RE.match(name):
        raise HTTPException(status_code=422, detail="Invalid model name")
    if not whisper.start_model_download(name):
        raise HTTPException(status_code=409, detail="This model is already being downloaded")
    return {"started": True, "name": name}


@router.delete("")
def delete_model(name: str, user: dict = AdminUser) -> dict:
    try:
        whisper.delete_model(name)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True}


# ── local LLM (llama.cpp) ─────────────────────────────────────────────


@router.get("/llm")
def llm_status() -> dict:
    """Hardware probe, recommendation, catalog, installed GGUF models."""
    return llamacpp.status()


@router.post("/llm/setup", status_code=202)
def install_llm_binary(body: SetupRequest | None = None, user: dict = AdminUser) -> dict:
    """Install the llama.cpp server in the background.

    `force` walks the installation ladder again over an existing installation
    — the way a build that computes on the processor is replaced by one that
    uses the graphics card, without deleting anything by hand first.
    """
    force = bool(body and body.force)
    if llamacpp.server_binary() is not None and not force:
        return {"started": False, "installed": True}
    if not llamacpp.start_binary_install(force=force):
        raise HTTPException(status_code=409, detail="Installation is already running")
    return {"started": True}


@router.delete("/llm/binary")
def uninstall_llm_binary(user: dict = AdminUser) -> dict:
    """Remove the llama.cpp installation; the downloaded models stay.

    Refused while an installation is running: the ladder unpacks and builds
    into that very directory, and deleting it underneath would leave a
    half-removed tree that `server_binary()` would happily try to start.
    """
    if llamacpp.install_running():
        raise HTTPException(status_code=409, detail="Es läuft gerade eine Installation.")
    llamacpp.uninstall_binary()
    llamacpp.reset_install_log()
    return {"deleted": True}


@router.post("/llm/download", status_code=202)
def download_llm_model(body: DownloadRequest, user: dict = AdminUser) -> dict:
    try:
        started = llamacpp.start_model_download(body.name.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not started:
        raise HTTPException(status_code=409, detail="This model is already being downloaded")
    return {"started": True, "name": body.name.strip()}


@router.delete("/llm")
def delete_llm_model(name: str, user: dict = AdminUser) -> dict:
    try:
        llamacpp.delete_model(name)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"deleted": True}


@router.post("/llm/stop")
def stop_llm_server(user: dict = AdminUser) -> dict:
    """Stop the managed llama-server (frees VRAM/RAM)."""
    llamacpp.stop_server()
    return {"stopped": True}


# ── speaker recognition (sherpa-onnx) ─────────────────────────────────


@router.get("/speakers")
def speaker_status() -> dict:
    """Library, the two models, what is being downloaded right now."""
    return diarize.status()


@router.post("/speakers/download", status_code=202)
def download_speaker_model(body: DownloadRequest, user: dict = AdminUser) -> dict:
    """Fetch the segmentation model or one embedding model."""
    name = body.name.strip()
    try:
        started = diarize.start_download(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not started:
        raise HTTPException(status_code=409, detail="Dieses Modell wird bereits geladen")
    return {"started": True, "name": name}


@router.delete("/speakers")
def delete_speaker_model(name: str, user: dict = AdminUser) -> dict:
    try:
        diarize.delete_model(name.strip())
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True}
