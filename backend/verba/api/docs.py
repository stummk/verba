"""Serve the in-app user documentation (markdown, one file per UI language)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import config

router = APIRouter(prefix="/api/docs", tags=["docs"])

DOCS_DIR = config.bundle_root() / "docs" / "user"
SUPPORTED = ("de", "en", "ru")
DEFAULT = "de"


@router.get("")
def get_docs(lang: str = DEFAULT) -> dict:
    """Return the user guide for the requested language (fallback: German)."""
    selected = lang if lang in SUPPORTED else DEFAULT
    path = DOCS_DIR / f"{selected}.md"
    if not path.exists():
        path = DOCS_DIR / f"{DEFAULT}.md"
        selected = DEFAULT
    if not path.exists():
        raise HTTPException(status_code=404, detail="Documentation not found")
    return {"lang": selected, "content": path.read_text(encoding="utf-8")}
