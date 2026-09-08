"""Segment editing, transcribing selections, and cutting the recording itself."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.jobs import job_queue
from ..services import audio, timeline, transcripts, vectorstore
from .deps import file_or_403 as _file_or_404

router = APIRouter(prefix="/api", tags=["segments"])


def _segment_or_404(segment_id: int, request: Request) -> dict:
    """The segment, if the caller may reach the transcript it belongs to."""
    segment = transcripts.get_segment(segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment nicht gefunden")
    _file_or_404(segment["file_id"], request)
    return segment


class SegmentCreate(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str = ""
    speaker: str = ""


@router.post("/files/{file_id}/segments")
def create_segment(file_id: int, body: SegmentCreate, request: Request) -> dict:
    """Add one segment to a transcript — usually empty, for a missed passage."""
    _file_or_404(file_id, request)
    if body.end_s <= body.start_s:
        raise HTTPException(status_code=422, detail="Ende muss nach dem Anfang liegen")
    segment = transcripts.create_segment(
        file_id, body.start_s, body.end_s, text=body.text, speaker=body.speaker
    )
    vectorstore.maybe_enqueue_index(file_id)
    return segment


class SegmentUpdate(BaseModel):
    text: str | None = None
    speaker: str | None = None
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, ge=0)


@router.put("/segments/{segment_id}")
def update_segment(segment_id: int, body: SegmentUpdate, request: Request) -> dict:
    _segment_or_404(segment_id, request)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    segment = transcripts.update_segment(segment_id, changes)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    # editor edits re-index only the affected file (deduplicated per queue)
    vectorstore.maybe_enqueue_index(segment["file_id"])
    return segment


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: int, request: Request) -> dict:
    segment = _segment_or_404(segment_id, request)
    if not transcripts.delete_segment(segment_id):
        raise HTTPException(status_code=404, detail="Segment not found")
    vectorstore.maybe_enqueue_index(segment["file_id"])
    return {"deleted": True}


class TimeSpan(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


class RangeRequest(BaseModel):
    """The passages to transcribe — one per selection on the waveform."""

    ranges: list[TimeSpan] = Field(min_length=1, max_length=50)
    model: str = ""
    language: str = ""


@router.post("/files/{file_id}/transcribe-range")
def transcribe_range(file_id: int, body: RangeRequest, request: Request) -> dict:
    """Transcribe the selected passages — as text, in one job, in their order."""
    file_row = _file_or_404(file_id, request)
    spans = _spans_or_422(body.ranges, file_row)
    payload: dict = {"ranges": [list(span) for span in spans]}
    if body.model:
        payload["model"] = body.model
    if body.language:
        payload["language"] = body.language
    return job_queue.enqueue(
        "transcribe_range", payload=payload, file_id=file_id, project_id=file_row["project_id"]
    )


class AudioCutRequest(BaseModel):
    """What is to be left of the recording, as spans of it (see services/timeline)."""

    keeps: list[TimeSpan] = Field(min_length=1, max_length=200)


@router.post("/files/{file_id}/audio/apply")
def apply_audio_cuts(file_id: int, body: AudioCutRequest, request: Request) -> dict:
    """Write the collected cuts into the recording — one pass, in place.

    The editor holds its cuts until the user asks for this, so what arrives
    here is not "remove that passage" but the whole result: the spans of the
    recording that survive. That makes the call idempotent and keeps a
    half-finished sequence of edits out of the file.
    """
    file_row = _file_or_404(file_id, request)
    keeps = _spans_or_422(body.keeps, file_row)
    if timeline.covers_all(keeps, file_row.get("duration")):
        raise HTTPException(status_code=422, detail="An dieser Aufnahme ist nichts geschnitten")
    return job_queue.enqueue(
        "audio_edit",
        payload={"keeps": [list(span) for span in keeps]},
        file_id=file_id,
        project_id=file_row["project_id"],
    )


@router.get("/files/{file_id}/audio/original")
def original_state(file_id: int, request: Request) -> dict:
    """Whether this recording can be put back the way it was imported."""
    file_row = _file_or_404(file_id, request)
    return {"can_restore": audio.has_original(file_row)}


@router.post("/files/{file_id}/audio/restore")
def restore_audio(file_id: int, request: Request) -> dict:
    """Undo every cut ever applied — the audio and the transcript with it."""
    file_row = _file_or_404(file_id, request)
    if not audio.has_original(file_row):
        raise HTTPException(status_code=404, detail="Von dieser Aufnahme liegt keine Sicherung vor")
    return job_queue.enqueue("audio_restore", file_id=file_id, project_id=file_row["project_id"])


def _spans_or_422(spans: list[TimeSpan], file_row: dict) -> list[tuple[float, float]]:
    """The selections as sorted, merged, in-range spans — or a plain refusal.

    Every one of them comes from a drag on a waveform, so overlapping and
    touching selections are normal input, not an error; what is worth refusing
    is a span that says nothing (end before start) or lies outside the
    recording, because then the caller and the file disagree about the file.
    """
    for span in spans:
        if span.end_s <= span.start_s:
            raise HTTPException(status_code=422, detail="Ende muss nach dem Anfang liegen")
    duration = file_row.get("duration") or 0.0
    normalized = timeline.normalize(
        [(span.start_s, span.end_s) for span in spans], duration or None
    )
    if not normalized:
        raise HTTPException(status_code=422, detail="Die Auswahl liegt nicht in der Aufnahme")
    return normalized
