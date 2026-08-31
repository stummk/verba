"""File import, server file browser, transcription triggers, segments, audio."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import config
from ..core.jobs import job_queue
from ..services import pipeline, transcripts, workspace
from ..services.llm import llm_location
from ..services.media import is_audio_file

router = APIRouter(prefix="/api", tags=["files"])


def _project_or_404(project_id: int) -> dict:
    project = workspace.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return project


def _file_or_404(file_id: int) -> dict:
    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return file_row


# ── server file browser (restricted to configured roots) ─────────────


def _browse_roots() -> list[Path]:
    configured = config.get_settings().general.browse_roots
    roots = [Path(r).resolve() for r in configured if Path(r).is_dir()]
    return roots or [Path.home().resolve()]


def _ensure_within_roots(path: Path) -> None:
    roots = _browse_roots()
    if not any(path == root or root in path.parents for root in roots):
        raise HTTPException(
            status_code=403,
            detail="Path is outside the permitted directories",
        )


@router.get("/files/browse")
def browse(path: str = "") -> dict:
    """List directories and audio files below the configured browse roots."""
    if not path:
        roots = _browse_roots()
        return {
            "path": "",
            "parent": None,
            "dirs": [{"name": str(r), "path": str(r)} for r in roots],
            "files": [],
        }

    current = Path(path).resolve()
    _ensure_within_roots(current)
    if not current.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    dirs, files = [], []
    try:
        for entry in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
            elif is_audio_file(entry):
                files.append({"name": entry.name, "path": str(entry), "size": entry.stat().st_size})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Access to this directory is denied") from exc

    parent: str | None = str(current.parent) if current != current.parent else None
    if parent is not None:
        parent_path = Path(parent)
        roots = _browse_roots()
        if not any(parent_path == root or root in parent_path.parents for root in roots):
            parent = ""  # "" navigates back to the roots overview
    return {"path": str(current), "parent": parent, "dirs": dirs, "files": files}


# ── import ────────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    paths: list[str]


@router.post("/projects/{project_id}/files/import")
def import_files(project_id: int, body: ImportRequest) -> list[dict]:
    project = _project_or_404(project_id)
    for raw in body.paths:
        _ensure_within_roots(Path(raw).resolve())
    imported = workspace.import_paths(project, body.paths)
    if not imported:
        raise HTTPException(status_code=422, detail="No audio files found in the selection")
    return imported


@router.post("/projects/{project_id}/files/upload")
async def upload_file(project_id: int, file: UploadFile) -> dict:
    project = _project_or_404(project_id)
    if not file.filename or not is_audio_file(Path(file.filename)):
        raise HTTPException(status_code=422, detail="No supported audio file")
    return workspace.save_upload(project, file.filename, file.file)


@router.delete("/files/{file_id}")
def delete_file(file_id: int) -> dict:
    _file_or_404(file_id)
    workspace.delete_file(file_id)
    return {"deleted": True}


# ── transcription ─────────────────────────────────────────────────────


class TranscribeOptions(BaseModel):
    """Per-flow overrides from the advanced panel; empty = use settings."""

    model: str = ""
    language: str = ""

    def as_payload(self) -> dict:
        return {key: value for key, value in self.model_dump().items() if value}


@router.post("/files/{file_id}/transcribe")
def transcribe_file(
    file_id: int,
    body: TranscribeOptions | None = None,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> dict:
    file_row = _file_or_404(file_id)
    if file_row["status"] == "transcribing":
        raise HTTPException(status_code=409, detail="File is already being transcribed")
    payload = body.as_payload() if body else {}
    return job_queue.enqueue(
        "transcribe",
        payload=payload,
        file_id=file_id,
        project_id=file_row["project_id"],
        session_id=x_session_id,
    )


@router.post("/projects/{project_id}/transcribe")
def transcribe_project(
    project_id: int,
    force: bool = False,
    body: TranscribeOptions | None = None,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> list[dict]:
    _project_or_404(project_id)
    skip_states = ("transcribing",) if force else ("transcribing", "done")
    payload = body.as_payload() if body else {}
    jobs = [
        job_queue.enqueue(
            "transcribe",
            payload=payload,
            file_id=f["id"],
            project_id=project_id,
            session_id=x_session_id,
        )
        for f in workspace.list_files(project_id)
        if f["status"] not in skip_states
    ]
    if not jobs:
        raise HTTPException(status_code=422, detail="No files to transcribe")
    return jobs


# ── LLM post-processing ───────────────────────────────────────────────


class ProcessOptions(BaseModel):
    """Pipeline steps for one file; model empty = configured default."""

    steps: list[str] = Field(default_factory=lambda: ["cleanup"])
    target_language: str = ""
    model: str = ""


def _ensure_llm_available() -> None:
    if llm_location() == "none":
        raise HTTPException(
            status_code=409,
            detail="No LLM configured — set one up in Settings",
        )


def _enqueue_process(file_row: dict, body: ProcessOptions, session_id: str) -> dict:
    # The same AI step twice on one file is never wanted: the second run would
    # only overwrite the first result and occupy the LLM lane for nothing. A
    # step that is not running yet still has to start — a translation asked for
    # while the cleanup runs used to be dropped without a word.
    steps = body.steps or ["cleanup"]
    steps, covering = pipeline.pending_steps(file_row["id"], steps, body.target_language)
    if not steps and covering is not None:
        return covering
    payload = {
        "file_id": file_row["id"],
        "steps": steps,
        "target_language": body.target_language,
        "model": body.model,
    }
    return job_queue.enqueue(
        "llm_process",
        payload=payload,
        file_id=file_row["id"],
        project_id=file_row["project_id"],
        session_id=session_id,
    )


@router.post("/files/{file_id}/process")
def process_file(
    file_id: int,
    body: ProcessOptions,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> dict:
    file_row = _file_or_404(file_id)
    _ensure_llm_available()
    if file_row["status"] != "done":
        raise HTTPException(status_code=409, detail="File has not been transcribed yet")
    return _enqueue_process(file_row, body, x_session_id)


@router.post("/projects/{project_id}/process")
def process_project(
    project_id: int,
    body: ProcessOptions,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> list[dict]:
    _project_or_404(project_id)
    _ensure_llm_available()
    jobs = [
        _enqueue_process(f, body, x_session_id)
        for f in workspace.list_files(project_id)
        if f["status"] == "done"
    ]
    if not jobs:
        raise HTTPException(status_code=422, detail="No transcribed files available")
    return jobs


@router.get("/files/{file_id}/texts")
def get_texts(file_id: int) -> dict:
    file_row = _file_or_404(file_id)
    return {"file": file_row, "texts": pipeline.list_texts(file_id)}


class TextUpdate(BaseModel):
    content: str = Field(max_length=2_000_000)


class FileHeaderUpdate(BaseModel):
    header_left: str = Field(default="", max_length=500)
    header_middle: str = Field(default="", max_length=500)
    header_right: str = Field(default="", max_length=500)


@router.put("/files/{file_id}/header")
def update_file_header(file_id: int, body: FileHeaderUpdate) -> dict:
    _file_or_404(file_id)
    updated = workspace.update_file(file_id, body.model_dump())
    assert updated is not None
    return updated


@router.put("/files/{file_id}/texts/{kind}")
def update_text(file_id: int, kind: str, body: TextUpdate, language: str = "") -> dict:
    """Manual edit of a derived text (cleanup/translation) from the editor."""
    _file_or_404(file_id)
    updated = pipeline.update_text_content(file_id, kind, language, body.content)
    if updated is None:
        raise HTTPException(status_code=404, detail="No such AI text available")
    return updated


# ── results ───────────────────────────────────────────────────────────


@router.get("/files/{file_id}/segments")
def get_segments(file_id: int) -> dict:
    file_row = _file_or_404(file_id)
    return {"file": file_row, "segments": transcripts.list_segments(file_id)}


@router.get("/files/{file_id}/audio")
def get_audio(file_id: int) -> FileResponse:
    file_row = _file_or_404(file_id)
    path = workspace.file_path(file_row)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio file is missing from the workspace")
    return FileResponse(path, filename=file_row["filename"])
