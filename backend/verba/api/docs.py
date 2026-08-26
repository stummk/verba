"""Serve the in-app user documentation (markdown, one file per UI language).

Besides the guide itself the router answers questions about it: one question,
one answer, no chat history — see services/docs_qa.py. The endpoint only
exists as a usable feature while an LLM is configured; the UI hides the
question box based on `llm_available` from GET /api/docs.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import docs_qa
from ..services.llm import LLMError

router = APIRouter(prefix="/api/docs", tags=["docs"])

SUPPORTED = docs_qa.SUPPORTED
DEFAULT = docs_qa.DEFAULT_LANG


@router.get("")
def get_docs(lang: str = DEFAULT) -> dict:
    """Return the user guide for the requested language (fallback: German)."""
    path = docs_qa.guide_path(lang)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Documentation not found")
    return {
        "lang": path.stem,
        "content": path.read_text(encoding="utf-8"),
        "llm_available": docs_qa.available(),
    }


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    lang: str = Field(default=DEFAULT, max_length=10)


@router.post("/ask")
def ask(body: AskRequest) -> dict:
    """Answer one question from the guide (no chat history, fresh context)."""
    if not docs_qa.available():
        raise HTTPException(
            status_code=409,
            detail="Kein Sprachmodell konfiguriert — die Hilfe-Fragen brauchen eines.",
        )
    try:
        return docs_qa.ask(body.question.strip(), body.lang)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
