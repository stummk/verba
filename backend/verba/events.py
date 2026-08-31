"""EventHub: broadcasts JSON events to the connected WebSocket clients.

Workers run in threads (pip installs, downloads, transcription jobs), so
`publish()` is thread-safe and marshals onto the server's event loop.

Events that talk about a specific transcript carry its `project_id` (or the
`file_id` it belongs to). While the user management is switched on those are
only delivered to clients that may see that transcript — otherwise the status
line would name the files of somebody else's private work. Who may see what is
resolved in `publish()`, i.e. still on the worker thread: the broadcast itself
must not run database queries on the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class EventHub:
    def __init__(self) -> None:
        # value: the logged-in user of that socket, or None (auth switched off)
        self._clients: dict[WebSocket, dict[str, Any] | None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket, user: dict[str, Any] | None = None) -> None:
        await ws.accept()
        self._clients[ws] = user

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        audience: dict[str, Any] | None = None,
    ) -> None:
        message = json.dumps({"type": event_type, "data": data or {}}, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws, user in list(self._clients.items()):
            if audience is not None and not _may_see(audience, user):
                continue
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def publish(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        project_id: int | None = None,
        file_id: int | None = None,
    ) -> None:
        """Thread-safe fire-and-forget broadcast (usable from worker threads).

        `project_id`/`file_id` scope the event to the transcript it is about;
        without either it goes to everyone (setup progress, engine status —
        nothing that names a user's material).
        """
        if self._loop is None or self._loop.is_closed():
            logger.debug("EventHub: no loop bound, dropping event %s", event_type)
            return
        audience = _resolve_audience(project_id, file_id)
        asyncio.run_coroutine_threadsafe(self.broadcast(event_type, data, audience), self._loop)


def _resolve_audience(project_id: int | None, file_id: int | None) -> dict[str, Any] | None:
    """Owner, visibility and share list of the transcript an event is about.

    None means "no restriction" — either the event is not about a transcript
    or the user management is off.
    """
    from . import db
    from .services import auth

    if not auth.enabled() or (project_id is None and file_id is None):
        return None
    with db.get_conn() as conn:
        if project_id is None:
            row = conn.execute("SELECT project_id FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                return {"owner_id": None, "visibility": "private", "shared_with": set()}
            project_id = row["project_id"]
        project = conn.execute(
            "SELECT owner_id, visibility FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            return {"owner_id": None, "visibility": "private", "shared_with": set()}
        shared = {
            r["user_id"]
            for r in conn.execute(
                "SELECT user_id FROM project_shares WHERE project_id = ?", (project_id,)
            )
        }
    return {
        "owner_id": project["owner_id"],
        "visibility": project["visibility"] or "public",
        "shared_with": shared,
    }


def _may_see(audience: dict[str, Any], user: dict[str, Any] | None) -> bool:
    if user is None:
        return False
    if user.get("role") == "admin" or audience["owner_id"] == user["id"]:
        return True
    if audience["visibility"] == "public":
        return True
    return audience["visibility"] == "shared" and user["id"] in audience["shared_with"]


hub = EventHub()
