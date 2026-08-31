"""User administration — administrators only.

There is no self-registration: accounts are created here, with a start
password the new user has to replace at their first login.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import auth
from .deps import AdminUser, SessionUser

router = APIRouter(prefix="/api/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(default="", max_length=200)
    role: str = Field(default=auth.ROLE_USER)


class UpdateUserRequest(BaseModel):
    """Partial update: only fields that were actually sent are changed."""

    display_name: str | None = Field(default=None, max_length=200)
    role: str | None = None
    password: str | None = Field(default=None, max_length=1024)


@router.get("")
def list_users(user: dict = AdminUser) -> list[dict]:
    return auth.list_users()


@router.get("/directory")
def user_directory(user: dict = SessionUser) -> list[dict]:
    """Just the names, for the share picker.

    Any logged-in user needs this: sharing a transcript with a colleague is
    impossible if you cannot see who exists. Roles, timestamps and how much
    each of them owns stay with the administrators.
    """
    return [
        {"id": entry["id"], "username": entry["username"], "display_name": entry["display_name"]}
        for entry in auth.list_users()
    ]


@router.post("", status_code=201)
def create_user(body: CreateUserRequest, user: dict = AdminUser) -> dict:
    try:
        created = auth.create_user(
            body.username,
            body.password,
            role=body.role,
            display_name=body.display_name,
            must_change_password=True,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return created


@router.put("/{user_id}")
def update_user(user_id: int, body: UpdateUserRequest, user: dict = AdminUser) -> dict:
    try:
        updated = auth.update_user(
            user_id,
            display_name=body.display_name,
            role=body.role,
            password=body.password,
        )
    except auth.AuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Nutzer nicht gefunden")
    if body.password is not None:
        # an administrator handing out a new password hands out a temporary
        # one; the account has to replace it at the next login
        auth.set_must_change_password(user_id, True)
        updated = auth.get_user(user_id)
    return updated


@router.delete("/{user_id}")
def delete_user(user_id: int, user: dict = AdminUser) -> dict:
    """Delete an account: private transcripts go, shared and public ones are
    handed to the longest-serving remaining administrator."""
    try:
        return auth.delete_user(user_id)
    except auth.AuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
