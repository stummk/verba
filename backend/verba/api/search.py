"""Semantic search endpoints: hybrid search, RAG answers, index status."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .. import config
from ..services import hardware, rag, vectorstore
from ..services.llm import llm_location

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    project_id: int | None = None
    type_id: int | None = None
    language: str = Field(default="", max_length=10)
    speaker: str = Field(default="", max_length=200)
    date_from: str = Field(default="", max_length=10)
    date_to: str = Field(default="", max_length=10)
    # hits, not files: they are grouped per file afterwards, so a handful of
    # files with several hits each still fits in the default
    limit: int = Field(default=24, ge=1, le=50)

    def filters(self) -> dict:
        return {
            "project_id": self.project_id,
            "type_id": self.type_id,
            "language": self.language,
            "speaker": self.speaker,
            "date_from": self.date_from,
            "date_to": self.date_to,
        }


def _ensure_available() -> None:
    if not vectorstore.available():
        raise HTTPException(
            status_code=409,
            detail="Search components are not installed — run setup first",
        )


@router.post("")
def search(body: SearchRequest) -> dict:
    """Hit list grouped by file: one entry per file, its hits in timeline order."""
    _ensure_available()
    try:
        hits = vectorstore.search(body.query, body.filters(), limit=body.limit)
    except vectorstore.EmbeddingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "results": vectorstore.group_by_file(hits),
        "llm_available": llm_location() != "none",
    }


@router.post("/ask")
def ask(body: SearchRequest) -> dict:
    """RAG answer plus the same grouped hit list, so one call fills both."""
    _ensure_available()
    if llm_location() == "none":
        raise HTTPException(status_code=409, detail="No LLM configured — set one up in Settings")
    try:
        answer = rag.ask(body.query, body.filters(), limit=min(body.limit, 12))
    except vectorstore.EmbeddingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**answer, "results": vectorstore.group_by_file(answer["sources"])}


@router.get("/status")
def get_status() -> dict:
    # the UI shows the AI-answer button next to the search button, so it has to
    # know about the LLM before the first search
    return {**vectorstore.status(), "llm_available": llm_location() != "none"}


@router.get("/models")
def list_embedding_models() -> dict:
    """The selectable embedding models — a catalog, not free text.

    `present` says whether the model already lies in the models directory: the
    UI can then promise "no download" instead of guessing. `fit` says whether
    this machine has the memory for it — the index is built here, locally.
    """
    hw = hardware.probe()
    alternatives = [(entry.label, entry.size_mb) for entry in config.EMBEDDING_MODELS]
    return {
        "models": [
            {
                "name": entry.name,
                "label": entry.label,
                "dim": entry.dim,
                "size_mb": entry.size_mb,
                "languages": entry.languages,
                "speed": entry.speed,
                "present": vectorstore.model_present_locally(entry),
                "fit": hardware.check_embedding_model(
                    entry.size_mb, hw=hw, alternatives=alternatives
                ),
            }
            for entry in config.EMBEDDING_MODELS
        ],
        "default": config.DEFAULT_EMBEDDING_MODEL,
        "configured": config.get_settings().search.embedding_model,
        "cache_dir": str(config.embeddings_dir()),
        "hardware": hw,
    }


@router.post("/reindex")
def reindex(x_session_id: str = Header(default="", alias="X-Session-Id")) -> dict:
    _ensure_available()
    job = vectorstore.enqueue_reindex(session_id=x_session_id)
    if job is None:
        raise HTTPException(status_code=409, detail="Reindex is already running")
    return job
