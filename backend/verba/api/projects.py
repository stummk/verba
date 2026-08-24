"""Project management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import workspace

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type_id: int | None = None


class UpdateProjectRequest(BaseModel):
    """Partial update: only fields that were actually sent are changed."""

    type_id: int | None = None
    auto_process: bool | None = None
    auto_language: str | None = Field(default=None, max_length=10)


@router.get("")
def list_projects() -> list[dict]:
    return workspace.list_projects()


@router.post("", status_code=201)
def create_project(body: CreateProjectRequest) -> dict:
    return workspace.create_project(body.name.strip(), type_id=body.type_id)


@router.put("/{project_id}")
def update_project(project_id: int, body: UpdateProjectRequest) -> dict:
    changes = {
        key: (int(value) if isinstance(value, bool) else value)
        for key, value in body.model_dump().items()
        if key in body.model_fields_set
    }
    project = workspace.update_project(project_id, changes)
    if project is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return project


@router.get("/{project_id}")
def get_project(project_id: int) -> dict:
    project = workspace.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    project["files"] = workspace.list_files(project_id)
    return project


@router.delete("/{project_id}")
def delete_project(project_id: int, delete_files: bool = False) -> dict:
    if workspace.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    workspace.delete_project(project_id, delete_files=delete_files)
    return {"deleted": True}
