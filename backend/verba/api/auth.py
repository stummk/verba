"""Login, logout and self-service for the optional user management."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import config
from ..services import auth
from .deps import AdminUser, current_user, session_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class EnableRequest(BaseModel):
    """Credentials for the very first administrator.

    Empty when the switch is only going back on after having been off:
    the accounts from before are still there with their own passwords.
    """

    username: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=1024)
    display_name: str = Field(default="", max_length=200)


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class DeleteMeRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    """HttpOnly so no script can read it, SameSite=Lax so a foreign page
    cannot make the browser act as this user, and Secure whenever the browser
    is on https.

    `request.url.scheme` is the scheme the *browser* used, not the one of the
    hop to this process: uvicorn rewrites it from X-Forwarded-Proto, so TLS
    terminated at a reverse proxy is recognised as https. It trusts that
    header from 127.0.0.1 only — a proxy on another host needs
    FORWARDED_ALLOW_IPS, or `auth.cookie_secure = "always"`.
    """
    settings = config.get_settings()
    mode = settings.auth.cookie_secure
    secure = request.url.scheme == "https" if mode == "auto" else mode == "always"
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=settings.auth.session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


@router.get("/state")
def get_state(request: Request) -> dict:
    """What the app needs before it can decide between login screen and UI."""
    enabled = auth.enabled()
    return {
        "enabled": enabled,
        "has_users": auth.user_count() > 0,
        "user": current_user(request) if enabled else None,
        "default_visibility": config.get_settings().auth.default_visibility,
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    if not auth.enabled():
        raise HTTPException(status_code=409, detail="Die Nutzerverwaltung ist nicht aktiv.")
    user = auth.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Benutzername oder Passwort ist falsch.")
    _set_session_cookie(request, response, auth.create_session(user["id"]))
    auth.purge_expired_sessions()
    return {"user": user}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    auth.destroy_session(session_token(request))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"logged_out": True}


@router.post("/enable")
def enable(body: EnableRequest, request: Request, response: Response) -> dict:
    """Switch the protection on — creating the first administrator if needed.

    Callable while the app is still unprotected: at that point everyone
    reaching it is effectively an administrator anyway, and refusing here
    would leave no way to ever secure an existing installation — nor to undo
    a switch-off.
    """
    if not auth.user_count() and not (body.username.strip() and body.password):
        raise HTTPException(
            status_code=422,
            detail="Benutzername und Passwort für das erste Administratorkonto fehlen.",
        )
    try:
        result = auth.enable(body.username, body.password, body.display_name)
    except auth.AuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # only the freshly created first administrator is signed in right away;
    # after a re-enable everybody logs in with the password they already have
    if result["user"]:
        _set_session_cookie(request, response, auth.create_session(result["user"]["id"]))
    return result


@router.post("/disable")
def disable(response: Response, user: dict = AdminUser) -> dict:
    auth.disable()
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"enabled": False}


@router.post("/password")
def change_password(body: PasswordRequest, request: Request, response: Response) -> dict:
    """Change one's own password; every other session is ended by it."""
    user = _require_self(request)
    if not auth.check_password(user["id"], body.current_password):
        raise HTTPException(status_code=403, detail="Das aktuelle Passwort ist falsch.")
    try:
        auth.update_user(user["id"], password=body.new_password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # update_user ends every session of this user, including this one
    _set_session_cookie(request, response, auth.create_session(user["id"]))
    return {"changed": True}


@router.delete("/me")
def delete_own_account(body: DeleteMeRequest, request: Request, response: Response) -> dict:
    """Delete one's own account.

    Private transcripts go with it; shared and public ones stay and change
    hands to the longest-serving administrator, so nobody loses material they
    were working with.
    """
    user = _require_self(request)
    if not auth.check_password(user["id"], body.password):
        raise HTTPException(status_code=403, detail="Das Passwort ist falsch.")
    try:
        result = auth.delete_user(user["id"])
    except auth.AuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return result


def _require_self(request: Request) -> dict:
    """The logged-in user — self-service is meaningless without one."""
    if not auth.enabled():
        raise HTTPException(status_code=409, detail="Die Nutzerverwaltung ist nicht aktiv.")
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Bitte anmelden.")
    return user
