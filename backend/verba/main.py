"""FastAPI application factory: REST API, WebSocket and static frontend."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db
from .api import apikeys as apikeys_api
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
from .core.jobs import job_queue
from .events import hub
from .logging_setup import setup_logging
from .services import llamacpp
from .services.audio import handle_audio_edit_job
from .services.pdf import handle_export_job
from .services.pipeline import handle_llm_process_job
from .services.project_types import seed_builtin_types
from .services.public_api import handle_api_transcribe_job
from .services.vectorstore import handle_index_file_job, handle_reindex_job
from .services.whisper import handle_transcribe_job, handle_transcribe_range_job

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
    job_queue.start()
    logger.info("Verba %s started — data at %s", __version__, config.data_dir())
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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

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
        await hub.connect(ws)
        try:
            while True:
                await ws.receive_text()  # keepalive pings from the client
        except WebSocketDisconnect:
            hub.disconnect(ws)

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app
