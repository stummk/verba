"""Read and update the application settings.

The LLM API key is never returned in clear text: GET masks it, and PUT keeps
the stored key when the masked placeholder is sent back unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from .. import config
from ..services import llm, vectorstore

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


@router.put("")
def update_settings(body: dict) -> dict:
    current = config.get_settings()
    try:
        updated = config.Settings.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid settings: {exc}") from exc

    if updated.llm.api_key == API_KEY_MASK:
        updated.llm.api_key = current.llm.api_key
    # setup state is owned by the backend, not the settings form
    updated.setup = current.setup

    config.save_settings(updated)
    # a changed embedding model invalidates every stored vector → full reindex
    if updated.search.embedding_model != current.search.embedding_model and vectorstore.available():
        vectorstore.enqueue_reindex()
    return _masked(updated)


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
