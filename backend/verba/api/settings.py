"""Read and update the application settings.

The LLM API key is never returned in clear text: GET masks it, and PUT keeps
the stored key when the masked placeholder is sent back unchanged.

Two settings have consequences beyond the file they are stored in, so PUT
acts on them: a new embedding model invalidates every stored vector (full
reindex), and a new workspaces directory has to take the existing project
folders with it (move job). Both are reported back in the response.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ValidationError

from .. import config
from ..core.jobs import job_queue
from ..services import llm, vectorstore, workspace

router = APIRouter(prefix="/api/settings", tags=["settings"])

API_KEY_MASK = "••••••••"


def _masked(settings: config.Settings) -> dict:
    data = settings.model_dump()
    if settings.llm.api_key:
        data["llm"]["api_key"] = API_KEY_MASK
    return data


@router.get("")
def get_settings() -> dict:
    return _masked(config.get_settings())


@router.get("/paths")
def get_paths() -> dict:
    """The directories in use — the settings form shows the effective values.

    A configured path is stored absolute (see config.normalize_dir), so what
    is shown here is exactly where the data goes.
    """
    settings = config.get_settings()
    return {
        "data_dir": str(config.data_dir()),
        "workspaces_dir": str(config.workspaces_root(settings)),
        "workspaces_default": str(config.default_workspaces_dir()),
        "workspaces_configured": bool(settings.general.workspaces_dir),
        "models_dir": str(config.models_dir(settings)),
        "embeddings_dir": str(config.embeddings_dir(settings)),
        "embeddings_default": str(config.default_embeddings_dir()),
        "llm_models_dir": str(config.llm_models_dir(settings)),
        "llm_models_default": str(config.default_llm_models_dir()),
        "logs_dir": str(config.logs_dir()),
        "project_count": len(workspace.list_projects()),
    }


@router.put("")
def update_settings(
    body: dict, x_session_id: str = Header(default="", alias="X-Session-Id")
) -> dict:
    current = config.get_settings()
    try:
        updated = config.Settings.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid settings: {exc}") from exc

    if updated.llm.api_key == API_KEY_MASK:
        updated.llm.api_key = current.llm.api_key
    # setup state is owned by the backend, not the settings form
    updated.setup = current.setup

    old_root = config.workspaces_root(current)
    new_root = config.workspaces_root(updated)
    move = None
    if new_root != old_root:
        # refuse before storing anything: a name collision in the target is
        # the one case that cannot be resolved without asking the user
        plan = workspace.move_plan(new_root)
        if plan["conflicts"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Im Zielverzeichnis existieren bereits Ordner mit gleichem Namen: "
                    + ", ".join(plan["conflicts"])
                ),
            )
        move = {"root": str(new_root), "projects": len(plan["moves"])}

    config.save_settings(updated)
    # a changed embedding model invalidates every stored vector → full reindex
    reindex = (
        updated.search.embedding_model != current.search.embedding_model and vectorstore.available()
    )
    if reindex:
        vectorstore.enqueue_reindex(session_id=x_session_id)
    if move and move["projects"]:
        job_queue.enqueue("move_workspace", payload={"root": move["root"]}, session_id=x_session_id)

    data = _masked(updated)
    data["workspace_move"] = move
    data["reindex_started"] = reindex
    return data


class LLMTestRequest(BaseModel):
    base_url: str
    api_key: str = ""


@router.post("/llm/test")
def test_llm_endpoint(body: LLMTestRequest) -> dict:
    """Probe an OpenAI-compatible endpoint and list its models."""
    if not body.base_url.strip():
        raise HTTPException(status_code=422, detail="Base URL fehlt")
    api_key = body.api_key
    if api_key == API_KEY_MASK:
        api_key = config.get_settings().llm.api_key
    return llm.probe(body.base_url.strip(), api_key)
