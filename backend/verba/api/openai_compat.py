"""Public OpenAI-compatible API (/v1) for external clients.

Authentication: as soon as at least one API key exists (settings → API) — or
the user management is switched on — requests must send
"Authorization: Bearer <key>". Without either the endpoint is as open as the
rest of the app (local desktop usage).

The request runs through the same fair job queue as the UI (round-robin per
API key) and returns synchronously once the job is done. Extensions beyond
the OpenAI wire format: model "…+cleanup" and the "project_type" form field
run the LLM cleanup so clients receive polished text directly.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse

from ..core.jobs import job_queue
from ..services import auth, llm, project_types, public_api

router = APIRouter(prefix="/v1", tags=["public-api"])

RESPONSE_FORMATS = ("json", "text", "srt", "vtt", "verbose_json")
POLL_INTERVAL_S = 0.2
UPLOAD_CHUNK = 1024 * 1024


def _authorize(request: Request) -> str:
    """Returns the queue session id: fair scheduling per API key.

    Once the user management is on, a key is mandatory — an endpoint that
    transcribes for anyone who reaches the port would undo the login the rest
    of the app just gained.
    """
    if not public_api.keys_configured() and not auth.enabled():
        return "api:open"
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    key = public_api.verify_key(token)
    if key is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key (Authorization: Bearer <key>)",
        )
    return f"api:{key['id']}"


def _parse_model(value: str) -> tuple[str, bool]:
    """ "whisper-1"/"whisper" → configured default; "…+cleanup" requests LLM cleanup."""
    value = (value or "").strip()
    cleanup = value.endswith("+cleanup")
    value = value.removesuffix("+cleanup").strip()
    if value in ("", "whisper", "whisper-1"):
        return "", cleanup
    return value, cleanup


@router.post("/audio/transcriptions")
async def create_transcription(
    request: Request,
    file: Annotated[UploadFile, File()],
    model: Annotated[str, Form()] = "whisper-1",
    language: Annotated[str, Form()] = "",
    prompt: Annotated[str, Form()] = "",  # accepted for compatibility, unused
    response_format: Annotated[str, Form()] = "json",
    temperature: Annotated[float, Form()] = 0.0,  # accepted for compatibility, unused
    project_type: Annotated[str, Form()] = "",
    session_id: Annotated[str, Depends(_authorize)] = "",
) -> Any:
    if response_format not in RESPONSE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown response_format — allowed: {', '.join(RESPONSE_FORMATS)}",
        )

    whisper_model, cleanup = _parse_model(model)
    type_prompt = ""
    if project_type:
        type_row = project_types.get_type_by_key(project_type)
        if type_row is None:
            raise HTTPException(status_code=400, detail=f"Unknown transcript type: {project_type}")
        type_prompt = type_row["system_prompt"]
        cleanup = True  # a type only makes sense with cleanup applied
    if cleanup and llm.llm_location() == "none":
        raise HTTPException(
            status_code=400,
            detail="Processing requested, but no LLM is configured (Settings -> AI)",
        )

    suffix = Path(file.filename or "audio").suffix[:16] or ".bin"
    target = public_api.uploads_dir() / f"{uuid.uuid4().hex}{suffix}"
    with target.open("wb") as out:
        while chunk := await file.read(UPLOAD_CHUNK):
            out.write(chunk)

    try:
        job = job_queue.enqueue(
            "api_transcribe",
            payload={
                "audio_path": str(target),
                "language": language.strip(),
                "model": whisper_model,
                "cleanup": cleanup,
                "type_prompt": type_prompt,
            },
            session_id=session_id,
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise

    current = await _wait_for_job(request, job["id"])
    if current is None or current["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="Transcription was cancelled")
    if current["status"] == "failed":
        raise HTTPException(status_code=500, detail=current["error"] or "Transcription failed")

    result = public_api.pop_result(job["id"])
    if result is None:
        raise HTTPException(status_code=500, detail="Result is no longer available")
    return _format_response(result, response_format)


async def _wait_for_job(request: Request, job_id: int) -> dict[str, Any] | None:
    cancelled_for_disconnect = False
    while True:
        current = job_queue.get(job_id)
        if current is None or current["status"] not in ("queued", "running"):
            return current
        if not cancelled_for_disconnect and await request.is_disconnected():
            job_queue.cancel(job_id)  # client gone — free the queue slot
            cancelled_for_disconnect = True
        await asyncio.sleep(POLL_INTERVAL_S)


def _format_response(result: dict[str, Any], response_format: str) -> Any:
    if response_format == "text":
        return PlainTextResponse(result["text"])
    if response_format == "srt":
        return PlainTextResponse(
            public_api.to_srt(result["segments"]), media_type="application/x-subrip"
        )
    if response_format == "vtt":
        return PlainTextResponse(public_api.to_vtt(result["segments"]), media_type="text/vtt")
    if response_format == "verbose_json":
        return public_api.to_verbose_json(result)
    return {"text": result["text"]}
