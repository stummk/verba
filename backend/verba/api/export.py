"""PDF export endpoints: start export jobs, list/download/delete results."""

from __future__ import annotations

import os
import tempfile
import zipfile
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..services import pdf, workspace
from .deps import file_or_403 as _file_or_404
from .deps import project_or_403 as _project_or_404

router = APIRouter(prefix="/api", tags=["export"])


class ExportOptions(BaseModel):
    """language empty = original text (cleanup, else transcript).

    combine ignores `language` and puts the original plus every stored
    translation into one PDF, separated by a divider line. file_ids narrows a
    project export to a selection — still one PDF, only of those files.
    """

    language: str = Field(default="", max_length=10)
    combine: bool = False
    file_ids: list[int] = Field(default_factory=list)


@router.post("/files/{file_id}/export")
def export_file(
    file_id: int,
    request: Request,
    body: ExportOptions | None = None,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> dict:
    file_row = _file_or_404(file_id, request)
    if file_row["status"] != "done":
        raise HTTPException(status_code=409, detail="File has not been transcribed yet")
    options = body or ExportOptions()
    return pdf.enqueue_file_export(
        file_row, options.language, x_session_id, combine=options.combine
    )


@router.post("/projects/{project_id}/export")
def export_project(
    project_id: int,
    request: Request,
    body: ExportOptions | None = None,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> dict:
    project = _project_or_404(project_id, request)
    options = body or ExportOptions()
    files = workspace.list_files(project_id)
    # a selection is checked against this transcript: an id from somewhere else
    # must not slip into its PDF
    chosen = set(options.file_ids)
    if chosen and not chosen <= {f["id"] for f in files}:
        raise HTTPException(status_code=404, detail="Datei gehört nicht zu diesem Transkript")
    if not any(f["status"] == "done" and (not chosen or f["id"] in chosen) for f in files):
        raise HTTPException(status_code=422, detail="No transcribed files available")
    return pdf.enqueue_project_export(
        project["id"],
        options.language,
        x_session_id,
        combine=options.combine,
        file_ids=options.file_ids,
    )


@router.get("/projects/{project_id}/exports")
def list_exports(project_id: int, request: Request) -> list[dict]:
    return pdf.list_exports(_project_or_404(project_id, request))


def _export_path_or_404(project_id: int, name: str, request: Request):
    project = _project_or_404(project_id, request)
    directory = pdf.exports_dir(project).resolve()
    path = (directory / name).resolve()
    if path.parent != directory or path.suffix != ".pdf":
        raise HTTPException(status_code=403, detail="Invalid export name")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return path


@router.get("/projects/{project_id}/exports/{name}")
def download_export(project_id: int, name: str, request: Request) -> FileResponse:
    path = _export_path_or_404(project_id, name, request)
    return FileResponse(path, filename=path.name, media_type="application/pdf")


@router.get("/projects/{project_id}/exports.zip")
def download_exports(
    project_id: int,
    request: Request,
    names: Annotated[list[str], Query()] = [],  # noqa: B006 — FastAPI reads it, never mutates
) -> FileResponse:
    """Several PDFs in one download — a browser only asks once for a zip."""
    project = _project_or_404(project_id, request)
    if not names:
        raise HTTPException(status_code=422, detail="Keine Exporte ausgewählt")
    paths = [_export_path_or_404(project_id, name, request) for name in names]
    handle, bundle_path = tempfile.mkstemp(suffix=".zip")
    os.close(handle)
    # stored, not deflated: a PDF is compressed already, and packing one again
    # costs time without saving anything worth mentioning
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_STORED) as bundle:
        for path in paths:
            bundle.write(path, arcname=path.name)
    return FileResponse(
        bundle_path,
        filename=f"{project['slug']}-exports.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, bundle_path),  # gone once it is sent
    )


@router.delete("/projects/{project_id}/exports/{name}")
def delete_export(project_id: int, name: str, request: Request) -> dict:
    _export_path_or_404(project_id, name, request).unlink()
    return {"deleted": True}
