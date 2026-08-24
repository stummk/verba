"""Semantic search endpoints: hybrid search, RAG answers, index status."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..services import rag, vectorstore
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
    limit: int = Field(default=10, ge=1, le=50)

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
    _ensure_available()
    results = vectorstore.search(body.query, body.filters(), limit=body.limit)
    return {"results": results, "llm_available": llm_location() != "none"}


@router.post("/ask")
def ask(body: SearchRequest) -> dict:
    _ensure_available()
    if llm_location() == "none":
        raise HTTPException(status_code=409, detail="No LLM configured — set one up in Settings")
    return rag.ask(body.query, body.filters(), limit=min(body.limit, 12))


@router.get("/status")
def get_status() -> dict:
    return vectorstore.status()


@router.post("/reindex")
def reindex(x_session_id: str = Header(default="", alias="X-Session-Id")) -> dict:
    _ensure_available()
    job = vectorstore.enqueue_reindex(session_id=x_session_id)
    if job is None:
        raise HTTPException(status_code=409, detail="Reindex is already running")
    return job
