"""Job monitoring and cancellation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..core.jobs import job_queue
from ..services import auth, workspace
from .deps import current_user

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _visible(jobs: list[dict], request: Request) -> list[dict]:
    """Only jobs on transcripts this user may see.

    A job row carries the file name, so an unfiltered list would tell everyone
    what everyone else is working on. Jobs without a transcript (reindex, a
    workspace move) are administration and stay with the administrators.
    """
    user = current_user(request)
    if not auth.enabled() or auth.is_admin(user):
        return jobs
    allowed = {project["id"] for project in workspace.list_projects(user)}
    return [job for job in jobs if job.get("project_id") in allowed]


@router.get("")
def list_jobs(request: Request, active: bool = False) -> list[dict]:
    return _visible(job_queue.list_jobs(active_only=active), request)


@router.get("/queue")
def queue_overview(request: Request) -> dict:
    """Queued/running jobs per lane with positions and the LLM location."""
    overview = job_queue.queue_overview()
    overview["lanes"] = {lane: _visible(jobs, request) for lane, jobs in overview["lanes"].items()}
    return overview


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int, request: Request) -> dict:
    job = job_queue.get(job_id)
    if job is None or not _visible([job], request):
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if not job_queue.cancel(job_id):
        raise HTTPException(status_code=409, detail="Job is no longer running or does not exist")
    return {"cancelled": True}
