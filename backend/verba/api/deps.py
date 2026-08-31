"""Request-level authentication and authorisation helpers.

Every protected route asks for one of the dependencies here instead of
repeating the rules. The important property: while the user management is
switched off `current_user` returns None *and* every check below passes, so
the single-user desktop build runs through exactly the same code path as a
secured server — there is no second, unguarded branch that could drift apart
from the guarded one.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request

from ..services import auth, workspace

# Reachable without a session while the user management is on: the login
# itself, the state query the login screen needs, and the health probe a
# reverse proxy or systemd watches.
PUBLIC_API_PATHS = frozenset({"/api/auth/state", "/api/auth/login", "/api/auth/logout", "/health"})


def session_token(request: Request) -> str:
    return request.cookies.get(auth.COOKIE_NAME, "")


def current_user(request: Request) -> dict[str, Any] | None:
    """The logged-in user, or None (also: always None while auth is off).

    Resolved once per request and cached on the request state, because a
    single handler may consult it several times through different helpers.
    """
    if not auth.enabled():
        return None
    cached = getattr(request.state, "verba_user", ...)
    if cached is not ...:
        return cached
    user = auth.session_user(session_token(request))
    request.state.verba_user = user
    return user


def require_user(request: Request) -> dict[str, Any] | None:
    if not auth.enabled():
        return None
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Bitte anmelden.")
    return user


def require_admin(request: Request) -> dict[str, Any] | None:
    """Administrators only — settings, users, models, types, API keys.

    Normal users keep their language and the documentation; everything that
    reaches beyond their own transcripts belongs to an administrator.
    """
    user = require_user(request)
    if auth.enabled() and not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Dafür sind Administratorrechte erforderlich.")
    return user


AdminUser = Depends(require_admin)
CurrentUser = Depends(current_user)
SessionUser = Depends(require_user)


# ── project-scoped access ─────────────────────────────────────────────


def project_or_403(project_id: int, request: Request) -> dict[str, Any]:
    """The project, if this user may use it.

    A transcript the user may not see answers 404, not 403: whether it exists
    at all is already more than they are entitled to know.
    """
    project = workspace.get_project(project_id)
    if project is None or not auth.can_access(project, current_user(request)):
        raise HTTPException(status_code=404, detail="Transkript nicht gefunden")
    return project


def file_or_403(file_id: int, request: Request) -> dict[str, Any]:
    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    project_or_403(file_row["project_id"], request)
    return file_row


def require_project_admin(project_id: int, request: Request) -> dict[str, Any]:
    """For owner, visibility and sharing — narrower than plain access."""
    project = project_or_403(project_id, request)
    if not auth.can_administer(project, current_user(request)):
        raise HTTPException(
            status_code=403,
            detail=(
                "Sichtbarkeit und Freigaben kann nur der Eigentümer oder ein Administrator ändern."
            ),
        )
    return project
