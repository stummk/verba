"""FastAPI application factory: REST API, WebSocket and static frontend."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, config, db, lifecycle
from .api import apikeys as apikeys_api
from .api import auth as auth_api
from .api import docs as docs_api
from .api import export as export_api
from .api import files as files_api
from .api import jobs as jobs_api
from .api import models as models_api
from .api import openai_compat as openai_compat_api
from .api import projects as projects_api
from .api import search as search_api
from .api import segments as segments_api
from .api import settings as settings_api
from .api import system as system_api
from .api import types as types_api
from .api import users as users_api
from .api.deps import PUBLIC_API_PATHS
from .core.jobs import job_queue
from .events import hub
from .logging_setup import setup_logging
from .services import auth, llamacpp
from .services.audio import handle_audio_edit_job
from .services.pdf import handle_export_job
from .services.pipeline import handle_llm_process_job
from .services.project_types import seed_builtin_types
from .services.public_api import handle_api_transcribe_job
from .services.vectorstore import handle_index_file_job, handle_reindex_job
from .services.whisper import handle_transcribe_job, handle_transcribe_range_job
from .services.workspace import handle_move_workspace_job

logger = logging.getLogger(__name__)

FRONTEND_DIR = config.bundle_root() / "frontend"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    hub.bind_loop(asyncio.get_running_loop())
    db.init_db()
    seed_builtin_types()
    job_queue.register("transcribe", handle_transcribe_job)
    job_queue.register("transcribe_range", handle_transcribe_range_job)
    job_queue.register("audio_edit", handle_audio_edit_job)
    job_queue.register("llm_process", handle_llm_process_job)
    job_queue.register("export_pdf", handle_export_job)
    job_queue.register("index_file", handle_index_file_job)
    job_queue.register("reindex_search", handle_reindex_job)
    job_queue.register("api_transcribe", handle_api_transcribe_job)
    job_queue.register("move_workspace", handle_move_workspace_job)
    job_queue.start()
    # run.py records the bound address here, so the log line answers "which
    # port is it on?" for a service started long ago
    bind = os.environ.get("VERBA_BIND", "")
    logger.info(
        "Verba %s started — listening on %s, data at %s",
        __version__,
        bind or "(address unknown)",
        config.data_dir(),
    )
    yield
    job_queue.stop()
    llamacpp.stop_server()


def create_app() -> FastAPI:
    settings = config.get_settings()
    setup_logging(settings)

    app = FastAPI(title="Verba", version=__version__, lifespan=_lifespan)

    @app.middleware("http")
    async def no_stale_frontend(request, call_next):
        """Frontend files must revalidate (ETag/304) so app updates arrive
        without cache-busting; API responses stay untouched."""
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # Paths a user with an expired start password may still reach: everything
    # needed to change it, plus the settings read that gives the UI its language.
    PASSWORD_CHANGE_ALLOWED = frozenset({"/api/auth/password", "/api/settings"})

    @app.middleware("http")
    async def enforce_authentication(request, call_next):
        """No /api route answers anonymously while the user management is on.

        The per-route checks (who owns which transcript, who is an admin) sit
        on top of this. The middleware exists so that forgetting one of them
        on a new route cannot open a hole: the default is "no session, no
        answer". The static frontend stays open — it is only the shell that
        renders the login screen.
        """
        path = request.url.path
        if path.startswith("/api") and path not in PUBLIC_API_PATHS and auth.enabled():
            token = request.cookies.get(auth.COOKIE_NAME, "")
            # a session lookup is a database read; a long-running write job
            # must never be able to stall the event loop with it
            user = await run_in_threadpool(auth.session_user, token)
            if user is None:
                return JSONResponse({"detail": "Bitte anmelden."}, status_code=401)
            request.state.verba_user = user
            pending = user["must_change_password"] and not path.startswith("/api/auth")
            if pending and path not in PASSWORD_CHANGE_ALLOWED:
                return JSONResponse(
                    {
                        "detail": "Bitte zuerst ein eigenes Passwort vergeben.",
                        "code": "password_change_required",
                    },
                    status_code=403,
                )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    app.include_router(auth_api.router)
    app.include_router(users_api.router)
    app.include_router(system_api.router)
    app.include_router(settings_api.router)
    app.include_router(projects_api.router)
    app.include_router(types_api.router)
    app.include_router(files_api.router)
    app.include_router(export_api.router)
    app.include_router(segments_api.router)
    app.include_router(jobs_api.router)
    app.include_router(search_api.router)
    app.include_router(models_api.router)
    app.include_router(docs_api.router)
    app.include_router(apikeys_api.router)
    app.include_router(openai_compat_api.router)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        # the socket carries job progress and file names, so it needs the same
        # session as the REST API; the hub remembers the user to decide which
        # transcripts this client may hear about
        user = None
        if auth.enabled():
            user = await run_in_threadpool(auth.session_user, ws.cookies.get(auth.COOKIE_NAME, ""))
            if user is None:
                # accept first: only an established socket can carry a close
                # code, and the UI needs 4401 to tell "log in again" apart
                # from an ordinary connection drop
                await ws.accept()
                await ws.close(code=4401)
                return
        await hub.connect(ws, user)
        lifecycle.cancel_idle_watchdog()
        try:
            while True:
                await ws.receive_text()  # keepalive pings from the client
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(ws)
            if not hub.client_count:
                # desktop mode follows its UI: closing the tab (or the whole
                # browser) ends the process after a short grace period
                lifecycle.arm_idle_watchdog()

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
