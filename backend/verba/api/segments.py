"""Segment editing, range re-transcription and audio editing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core.jobs import job_queue
from ..services import transcripts, vectorstore, workspace
from ..services.audio import EDIT_OPS

router = APIRouter(prefix="/api", tags=["segments"])


def _file_or_404(file_id: int) -> dict:
    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return file_row


class SegmentUpdate(BaseModel):
    text: str | None = None
    speaker: str | None = None
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, ge=0)


@router.put("/segments/{segment_id}")
def update_segment(segment_id: int, body: SegmentUpdate) -> dict:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    segment = transcripts.update_segment(segment_id, changes)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    # editor edits re-index only the affected file (deduplicated per queue)
    vectorstore.maybe_enqueue_index(segment["file_id"])
    return segment


@router.delete("/segments/{segment_id}")
def delete_segment(segment_id: int) -> dict:
    segment = transcripts.get_segment(segment_id)
    if segment is None or not transcripts.delete_segment(segment_id):
        raise HTTPException(status_code=404, detail="Segment not found")
    vectorstore.maybe_enqueue_index(segment["file_id"])
    return {"deleted": True}


class RangeRequest(BaseModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    model: str = ""
    language: str = ""


@router.post("/files/{file_id}/transcribe-range")
def transcribe_range(file_id: int, body: RangeRequest) -> dict:
    file_row = _file_or_404(file_id)
    if body.end_s <= body.start_s:
        raise HTTPException(status_code=422, detail="End must be after start")
    payload = {"start_s": body.start_s, "end_s": body.end_s}
    if body.model:
        payload["model"] = body.model
    if body.language:
        payload["language"] = body.language
    return job_queue.enqueue(
        "transcribe_range", payload=payload, file_id=file_id, project_id=file_row["project_id"]
    )


class AudioEditRequest(BaseModel):
    op: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)


@router.post("/files/{file_id}/audio/edit")
def edit_audio(file_id: int, body: AudioEditRequest) -> dict:
    file_row = _file_or_404(file_id)
    if body.op not in EDIT_OPS:
        raise HTTPException(status_code=422, detail=f"Unknown operation: {body.op}")
    if body.end_s <= body.start_s:
        raise HTTPException(status_code=422, detail="End must be after start")
    return job_queue.enqueue(
        "audio_edit",
        payload={"op": body.op, "start_s": body.start_s, "end_s": body.end_s},
        file_id=file_id,
        project_id=file_row["project_id"],
    )
