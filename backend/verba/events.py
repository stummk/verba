"""EventHub: broadcasts JSON events to all connected WebSocket clients.

Workers run in threads (pip installs, downloads, later: transcription jobs),
so `publish()` is thread-safe and marshals onto the server's event loop.
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
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        message = json.dumps({"type": event_type, "data": data or {}}, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Thread-safe fire-and-forget broadcast (usable from worker threads)."""
        if self._loop is None or self._loop.is_closed():
            logger.debug("EventHub: no loop bound, dropping event %s", event_type)
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(event_type, data), self._loop)


hub = EventHub()
