"""Split a transcript into LLM-sized chunks along segment boundaries.

Chunks never cut through a segment; consecutive chunks overlap by a few
segments so the model keeps local context at the seams. Overlapping segments
are only used as context — each segment belongs to exactly one chunk for
output purposes (`own_start` marks where a chunk's own content begins).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_CHARS = 6000  # conservative fit for small local context windows
DEFAULT_OVERLAP_SEGMENTS = 2


@dataclass
class Chunk:
    segments: list[dict[str, Any]]  # includes leading overlap segments
    own_start: int  # index into `segments` where non-overlap content begins

    @property
    def context_text(self) -> str:
        return " ".join(s["text"].strip() for s in self.segments[: self.own_start])

    @property
    def own_text(self) -> str:
        return "\n".join(s["text"].strip() for s in self.segments[self.own_start :])


def chunk_segments(
    segments: list[dict[str, Any]],
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP_SEGMENTS,
) -> list[Chunk]:
    if not segments:
        return []

    chunks: list[Chunk] = []
    current: list[dict[str, Any]] = []
    current_chars = 0

    for segment in segments:
        length = len(segment["text"]) + 1
        if current and current_chars + length > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += length
    if current:
        chunks.append(current)

    result: list[Chunk] = []
    for i, chunk_segments_ in enumerate(chunks):
        if i == 0 or overlap <= 0:
            result.append(Chunk(segments=chunk_segments_, own_start=0))
        else:
            lead = chunks[i - 1][-overlap:]
            result.append(Chunk(segments=lead + chunk_segments_, own_start=len(lead)))
    return result
