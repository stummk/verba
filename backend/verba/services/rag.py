"""RAG answers over the search index: every statement cites its sources.

The LLM only sees the retrieved passages and must answer from them alone;
with no hits there is no LLM call at all — the search says so honestly
instead of guessing. Without an LLM the plain hit list (vectorstore.search)
is the product; this module is only used when one is configured.
"""

from __future__ import annotations

from typing import Any

from . import llm, vectorstore

RAG_SYSTEM_PROMPT = (
    "You answer questions solely based on the numbered passages from transcripts. Cite "
    "every statement with the source number in square brackets, e.g. [1] or [2][3]. "
    "Do not use outside knowledge. If the passages do not answer the question, say so "
    "honestly. Answer in the language of the question."
)


# What a passage is: spoken text, header data, or one of the texts derived from
# the recording. The model has to be told — it would otherwise quote a name from
# the header as if someone had said it out loud, or present a translation as the
# words of the speaker.
_ORIGINS = {
    "header": "Kopfdaten",
    "cleanup": "geglätteter Text",
    "translation": "Übersetzung",
}


def _origin(source: dict[str, Any]) -> str:
    label = _ORIGINS.get(source.get("source", ""), "")
    if not label:
        return ""
    language = source.get("source_language") or ""
    return f", {label} {language}" if language else f", {label}"


def ask(query: str, filters: dict[str, Any] | None = None, limit: int = 8) -> dict[str, Any]:
    """Hybrid search, then an LLM answer grounded in the hits."""
    sources = vectorstore.search(query, filters, limit=limit)
    if not sources:
        return {"answer": "", "sources": []}

    passages = "\n\n".join(
        f"[{index + 1}] ({source['project_name']} — "
        f"{vectorstore.display_label(source)}{_origin(source)}) {source['text']}"
        for index, source in enumerate(sources)
    )
    answer = llm.chat(
        [
            {"role": "system", "content": RAG_SYSTEM_PROMPT},
            {"role": "user", "content": f"Passagen:\n{passages}\n\nFrage: {query}"},
        ]
    )
    return {"answer": answer.strip(), "sources": sources}
