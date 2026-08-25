"""Project type management endpoints (CRUD + restore defaults)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import project_types
from ..services.pdf import DEFAULT_STRUCTURE, STRUCTURES

router = APIRouter(prefix="/api/types", tags=["types"])


class TypeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    system_prompt: str = Field(default="", max_length=8000)
    # empty on create pre-fills the default; empty on update keeps the default
    # in effect at export time
    output_prompt: str = Field(default="", max_length=8000)
    structure: Literal[STRUCTURES] = DEFAULT_STRUCTURE


@router.get("")
def list_types() -> list[dict]:
    return project_types.list_types(include_legacy=False)


@router.get("/defaults")
def default_prompts() -> dict:
    """Defaults and choices the UI needs to offer a new type."""
    return {
        "output_prompt": project_types.default_output_prompt(),
        "structure": DEFAULT_STRUCTURE,
        "structures": list(STRUCTURES),
    }


@router.post("", status_code=201)
def create_type(body: TypeRequest) -> dict:
    return project_types.create_type(
        body.name.strip(),
        body.system_prompt.strip(),
        body.output_prompt.strip(),
        body.structure,
    )


@router.put("/{type_id}")
def update_type(type_id: int, body: TypeRequest) -> dict:
    updated = project_types.update_type(
        type_id,
        body.name.strip(),
        body.system_prompt.strip(),
        body.output_prompt.strip(),
        body.structure,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Transcript type not found")
    return updated


@router.delete("/{type_id}")
def delete_type(type_id: int) -> dict:
    if not project_types.delete_type(type_id):
        raise HTTPException(status_code=404, detail="Transcript type not found")
    return {"deleted": True}


@router.post("/restore-defaults")
def restore_defaults() -> list[dict]:
    return project_types.restore_builtin_types()
