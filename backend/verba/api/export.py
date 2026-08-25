"""PDF export endpoints: start export jobs, list/download/delete results."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..services import pdf, workspace

router = APIRouter(prefix="/api", tags=["export"])


class ExportOptions(BaseModel):
    """language empty = original text (cleanup, else transcript).

    combine ignores `language` and puts the original plus every stored
    translation into one PDF, separated by a divider line.
    """

    language: str = Field(default="", max_length=10)
    combine: bool = False


def _project_or_404(project_id: int) -> dict:
    project = workspace.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return project


@router.post("/files/{file_id}/export")
def export_file(
    file_id: int,
    body: ExportOptions | None = None,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> dict:
    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")
    if file_row["status"] != "done":
        raise HTTPException(status_code=409, detail="File has not been transcribed yet")
    options = body or ExportOptions()
    return pdf.enqueue_file_export(
        file_row, options.language, x_session_id, combine=options.combine
    )


@router.post("/projects/{project_id}/export")
def export_project(
    project_id: int,
    body: ExportOptions | None = None,
    x_session_id: str = Header(default="", alias="X-Session-Id"),
) -> dict:
    project = _project_or_404(project_id)
    if not any(f["status"] == "done" for f in workspace.list_files(project_id)):
        raise HTTPException(status_code=422, detail="No transcribed files available")
    options = body or ExportOptions()
    return pdf.enqueue_project_export(
        project["id"], options.language, x_session_id, combine=options.combine
    )


@router.get("/projects/{project_id}/exports")
def list_exports(project_id: int) -> list[dict]:
    return pdf.list_exports(_project_or_404(project_id))


def _export_path_or_404(project_id: int, name: str):
    project = _project_or_404(project_id)
    directory = pdf.exports_dir(project).resolve()
    path = (directory / name).resolve()
    if path.parent != directory or path.suffix != ".pdf":
        raise HTTPException(status_code=403, detail="Invalid export name")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return path


@router.get("/projects/{project_id}/exports/{name}")
def download_export(project_id: int, name: str) -> FileResponse:
    path = _export_path_or_404(project_id, name)
    return FileResponse(path, filename=path.name, media_type="application/pdf")


@router.delete("/projects/{project_id}/exports/{name}")
def delete_export(project_id: int, name: str) -> dict:
    _export_path_or_404(project_id, name).unlink()
    return {"deleted": True}
