"""Job monitoring and cancellation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.jobs import job_queue

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def list_jobs(active: bool = False) -> list[dict]:
    return job_queue.list_jobs(active_only=active)


@router.get("/queue")
def queue_overview() -> dict:
    """Queued/running jobs per lane with positions and the LLM location."""
    return job_queue.queue_overview()


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    if not job_queue.cancel(job_id):
        raise HTTPException(status_code=409, detail="Job is no longer running or does not exist")
    return {"cancelled": True}
