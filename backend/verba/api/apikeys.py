"""API key management for the public /v1 endpoint (settings page)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import public_api
from .deps import AdminUser

router = APIRouter(prefix="/api/apikeys", tags=["apikeys"])


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@router.get("")
def list_keys(user: dict = AdminUser) -> list[dict]:
    return public_api.list_keys()


@router.post("", status_code=201)
def create_key(body: KeyRequest, user: dict = AdminUser) -> dict:
    # The response contains the plaintext key exactly once.
    return public_api.create_key(body.name.strip())


@router.delete("/{key_id}")
def delete_key(key_id: int, user: dict = AdminUser) -> dict:
    if not public_api.delete_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"deleted": True}
