"""Project management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..services import auth, workspace
from .deps import current_user, project_or_403, require_project_admin

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type_id: int | None = None
    # empty = the default configured for this installation
    visibility: str = Field(default="", max_length=20)


class UpdateProjectRequest(BaseModel):
    """Partial update: only fields that were actually sent are changed.

    Owner and visibility are deliberately absent — they go through
    PUT /visibility, which is restricted to the owner and administrators.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    type_id: int | None = None
    auto_process: bool | None = None
    auto_language: str | None = Field(default=None, max_length=10)


class VisibilityRequest(BaseModel):
    visibility: str = Field(max_length=20)
    # only meaningful for "shared"
    user_ids: list[int] = Field(default_factory=list)


def _with_shares(project: dict) -> dict:
    project["shared_with"] = (
        auth.list_shares(project["id"]) if project.get("visibility") == "shared" else []
    )
    return project


@router.get("")
def list_projects(request: Request) -> list[dict]:
    return workspace.list_projects(current_user(request))


@router.post("", status_code=201)
def create_project(body: CreateProjectRequest, request: Request) -> dict:
    user = current_user(request)
    if body.visibility and body.visibility not in auth.VISIBILITIES:
        raise HTTPException(status_code=422, detail=f"Unbekannte Sichtbarkeit: {body.visibility}")
    return workspace.create_project(
        body.name.strip(),
        type_id=body.type_id,
        owner_id=user["id"] if user else None,
        visibility=body.visibility,
    )


@router.put("/{project_id}")
def update_project(project_id: int, body: UpdateProjectRequest, request: Request) -> dict:
    project_or_403(project_id, request)
    changes = {
        key: (int(value) if isinstance(value, bool) else value)
        for key, value in body.model_dump().items()
        if key in body.model_fields_set
    }
    project = workspace.update_project(project_id, changes)
    if project is None:
        raise HTTPException(status_code=404, detail="Transkript nicht gefunden")
    return _with_shares(project)


@router.put("/{project_id}/visibility")
def update_visibility(project_id: int, body: VisibilityRequest, request: Request) -> dict:
    """Who may reach this transcript — owner and administrators only.

    Everyone who can see a transcript may edit it, so this one setting has to
    stay with its owner: otherwise a colleague could make a shared transcript
    private and lock the rest of the team out of their own material.
    """
    require_project_admin(project_id, request)
    try:
        project = workspace.set_visibility(project_id, body.visibility, body.user_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=404, detail="Transkript nicht gefunden")
    return _with_shares(project)


@router.get("/{project_id}")
def get_project(project_id: int, request: Request) -> dict:
    project = project_or_403(project_id, request)
    project["files"] = workspace.list_files(project_id)
    return _with_shares(project)


@router.delete("/{project_id}")
def delete_project(project_id: int, request: Request, delete_files: bool = True) -> dict:
    project_or_403(project_id, request)
    workspace.delete_project(project_id, delete_files=delete_files)
    return {"deleted": True}
