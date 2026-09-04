"""Project workspaces: one dedicated folder per project on disk.

Layout: <workspaces>/<slug>/
    project.json    project metadata (transparency for the user)
    audio/          imported copies of the audio files
    transcripts/    one JSON per transcribed file
    exports/        PDF exports (phase 5)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO

from .. import config, db
from ..core.jobs import JobCancelled
from ..events import hub
from . import maintenance
from .media import is_audio_file, probe_duration
from .metadata import extract_metadata, format_display_date

logger = logging.getLogger(__name__)

WORKSPACE_SUBDIRS = ("audio", "transcripts", "exports")


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name).strip("-").lower()
    return slug or "projekt"


def _unique_slug(conn, base: str) -> str:
    slug = base
    counter = 2
    while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def project_dir(project: dict[str, Any]) -> Path:
    return Path(project["workspace"])


def create_project(
    name: str,
    type_id: int | None = None,
    owner_id: int | None = None,
    visibility: str = "",
) -> dict[str, Any]:
    """Create a project folder and its database row.

    Without an explicit visibility the configured default applies — and while
    the user management is off that is always "public", because there is
    nobody the transcript could be private from.
    """
    settings = config.get_settings()
    if not visibility:
        visibility = settings.auth.default_visibility if settings.auth.enabled else "public"
    with db.get_conn() as conn:
        slug = _unique_slug(conn, slugify(name))
        workspace = config.workspaces_dir(settings) / slug
        # A new project processes automatically: the cleanup after a finished
        # transcription is what almost every project wants, and it is one click
        # to switch off. Existing projects keep whatever they were set to.
        cursor = conn.execute(
            "INSERT INTO projects (name, slug, workspace, type_id, owner_id, visibility, "
            "auto_process) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (name, slug, str(workspace), type_id, owner_id, visibility),
        )
        project_id = cursor.lastrowid

    for subdir in WORKSPACE_SUBDIRS:
        (workspace / subdir).mkdir(parents=True, exist_ok=True)
    (workspace / "project.json").write_text(
        json.dumps({"name": name, "slug": slug, "id": project_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return get_project(project_id)  # type: ignore[return-value]


def get_project(project_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT p.*, t.key AS type_key, t.name AS type_name, t.system_prompt AS type_prompt, "
            "t.output_prompt AS type_output_prompt, t.structure AS type_structure, "
            "u.username AS owner_name "
            "FROM projects p LEFT JOIN project_types t ON t.id = p.type_id "
            "LEFT JOIN users u ON u.id = p.owner_id "
            "WHERE p.id = ?",
            (project_id,),
        ).fetchone()
    return db.row_to_dict(row)


UPDATABLE_PROJECT_FIELDS = ("name", "type_id", "auto_process", "auto_language")


def update_project(project_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Update selected project fields (type, auto-processing); ignores unknown keys."""
    project = get_project(project_id)
    if project is None:
        return None
    fields = {k: v for k, v in changes.items() if k in UPDATABLE_PROJECT_FIELDS}
    if not fields:
        return project

    source_dir = Path(project["workspace"])
    target_dir: Path | None = None

    new_name = fields.get("name")
    if new_name is not None:
        cleaned = str(new_name).strip()
        if cleaned and cleaned != project["name"]:
            with db.get_conn() as conn:
                new_slug = _unique_slug(conn, slugify(cleaned))
            target_dir = source_dir.parent / new_slug
            if source_dir != target_dir:
                if target_dir.exists():
                    raise RuntimeError(f"Der Projektordner {target_dir} existiert bereits.")
                source_dir.parent.mkdir(parents=True, exist_ok=True)
                if source_dir.exists():
                    source_dir.rename(target_dir)
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                fields["slug"] = new_slug
                fields["workspace"] = str(target_dir)

    assignments = ", ".join(f"{key} = ?" for key in fields)
    with db.get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE projects SET {assignments} WHERE id = ?",  # noqa: S608 — whitelisted keys
            (*fields.values(), project_id),
        )
        if cursor.rowcount == 0:
            return None

    if target_dir is not None:
        final_dir = target_dir
        final_dir.mkdir(parents=True, exist_ok=True)
        (final_dir / "project.json").write_text(
            json.dumps(
                {
                    "name": fields.get("name", project["name"]),
                    "slug": fields.get("slug", project["slug"]),
                    "id": project_id,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return get_project(project_id)


def list_projects(user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every transcript this user may see (all of them while auth is off)."""
    from .auth import visibility_clause  # local import: auth imports this module

    clause, params = visibility_clause(user)
    where = f"WHERE {clause}" if clause else ""
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT p.*, t.name AS type_name, u.username AS owner_name,
                   COUNT(f.id) AS file_count,
                   SUM(CASE WHEN f.status = 'done' THEN 1 ELSE 0 END) AS done_count
            FROM projects p
            LEFT JOIN project_types t ON t.id = p.type_id
            LEFT JOIN users u ON u.id = p.owner_id
            LEFT JOIN files f ON f.project_id = p.id
            {where}
            GROUP BY p.id ORDER BY p.created_at DESC
            """,
            params,
        ).fetchall()
    return db.rows_to_dicts(rows)


def set_visibility(
    project_id: int, visibility: str, user_ids: list[int] | None = None
) -> dict[str, Any] | None:
    """Change who may reach a transcript.

    The share list is only kept for 'shared'; switching to private or public
    clears it, so a transcript that becomes shared again does not silently
    reuse a list nobody remembers.
    """
    from .auth import VISIBILITIES, set_shares

    if visibility not in VISIBILITIES:
        raise ValueError(f"Unbekannte Sichtbarkeit: {visibility}")
    if get_project(project_id) is None:
        return None
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET visibility = ? WHERE id = ?", (visibility, project_id))
    set_shares(project_id, user_ids or [] if visibility == "shared" else [])
    return get_project(project_id)


def set_owner(project_id: int, owner_id: int | None) -> dict[str, Any] | None:
    if get_project(project_id) is None:
        return None
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET owner_id = ? WHERE id = ?", (owner_id, project_id))
    return get_project(project_id)


def delete_project(project_id: int, delete_files: bool = True) -> None:
    project = get_project(project_id)
    if project is None:
        return
    from .vectorstore import remove_project  # local import: avoids a module cycle

    remove_project(project_id)  # search index entries disappear immediately
    with db.get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    if delete_files:
        shutil.rmtree(project_dir(project), ignore_errors=True)
    # segments, chunks and embeddings of a whole project leave a lot of free
    # space behind — give the file a chance to shrink back
    maintenance.request_vacuum()


# ── files ─────────────────────────────────────────────────────────────


# Every file row carries which derived texts exist for it ("cleanup",
# "translation"): only that tells the UI whether a file has already been
# through the AI step — its status stays "done" either way.
FILE_SELECT = (
    "SELECT f.*, (SELECT group_concat(DISTINCT kind) FROM derived_texts "
    "WHERE file_id = f.id AND trim(content) != '') AS derived_kinds FROM files f"
)


def list_files(project_id: int) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            f"{FILE_SELECT} WHERE f.project_id = ? ORDER BY f.filename", (project_id,)
        ).fetchall()
    return db.rows_to_dicts(rows)


def get_file(file_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(f"{FILE_SELECT} WHERE f.id = ?", (file_id,)).fetchone()
    return db.row_to_dict(row)


UPDATABLE_FILE_FIELDS = ("header_left", "header_middle", "header_right")


def update_file(file_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
    fields = {k: v for k, v in changes.items() if k in UPDATABLE_FILE_FIELDS}
    if not fields:
        return get_file(file_id)
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with db.get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE files SET {assignments} WHERE id = ?",  # noqa: S608 — whitelisted keys
            (*fields.values(), file_id),
        )
        if cursor.rowcount == 0:
            return None
    emit_file_update(file_id)
    return get_file(file_id)


def file_path(file_row: dict[str, Any]) -> Path:
    project = get_project(file_row["project_id"])
    assert project is not None
    return project_dir(project) / file_row["rel_path"]


def emit_file_update(file_id: int) -> None:
    file_row = get_file(file_id)
    if file_row is not None:
        hub.publish("file.update", file_row, project_id=file_row["project_id"])


def unique_target(audio_dir: Path, filename: str) -> Path:
    target = audio_dir / filename
    stem, suffix = target.stem, target.suffix
    counter = 2
    while target.exists():
        target = audio_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    return target


def register_file(project: dict[str, Any], target: Path, source: str = "") -> dict[str, Any]:
    rel_path = str(target.relative_to(project_dir(project)))
    duration = probe_duration(target)
    meta = extract_metadata(target)
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO files (project_id, filename, rel_path, source_path, duration, "
            "title, recorded_at, language, target_language, header_left, header_middle, "
            "header_right) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project["id"],
                target.name,
                rel_path,
                source,
                duration,
                meta["title"],
                format_display_date(meta["recorded_at"]),
                meta.get("language", ""),
                meta.get("target_language", ""),
                meta["title"] or target.stem,
                meta.get("addition", ""),
                meta["recorded_at"],
            ),
        )
        file_id = cursor.lastrowid
    file_row = get_file(file_id)
    assert file_row is not None
    hub.publish("file.update", file_row, project_id=file_row["project_id"])
    return file_row


def import_paths(project: dict[str, Any], paths: list[str]) -> list[dict[str, Any]]:
    """Copy audio files (or all audio files inside folders) into the workspace."""
    audio_dir = project_dir(project) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    sources: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            sources.extend(sorted(p for p in path.rglob("*") if p.is_file() and is_audio_file(p)))
        elif path.is_file() and is_audio_file(path):
            sources.append(path)

    imported: list[dict[str, Any]] = []
    for source in sources:
        target = unique_target(audio_dir, source.name)
        shutil.copy2(source, target)
        imported.append(register_file(project, target, source=str(source)))
    return imported


def save_upload(project: dict[str, Any], filename: str, stream: BinaryIO) -> dict[str, Any]:
    audio_dir = project_dir(project) / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    safe_name = PureWindowsPath(filename).name
    target = unique_target(audio_dir, safe_name)
    with open(target, "wb") as out:
        shutil.copyfileobj(stream, out)
    return register_file(project, target)


def _delete_generated_file_artifacts(file_row: dict[str, Any]) -> None:
    """Remove transcript and derived-text files generated for one file.

    PDF exports are not deleted here because they are project-level artifacts and
    can be kept even after the source audio file is removed.
    """
    project = get_project(file_row["project_id"])
    if project is None:
        return

    stem = Path(file_row["filename"]).stem
    project_root = project_dir(project)

    transcripts_dir = project_root / "transcripts"
    if transcripts_dir.is_dir():
        for candidate in list(transcripts_dir.iterdir()):
            name = candidate.name
            if name == f"{stem}.json" or (
                candidate.suffix in {".json", ".md"}
                and name.startswith(f"{stem}.")
                and not name.startswith(f"{stem}.export.")
            ):
                candidate.unlink(missing_ok=True)


def delete_file(file_id: int) -> None:
    """Remove the DB entry and the workspace copy (never the original source)."""
    file_row = get_file(file_id)
    if file_row is None:
        return
    path = file_path(file_row)
    _delete_generated_file_artifacts(file_row)
    from .vectorstore import remove_file  # local import: avoids a module cycle

    remove_file(file_id)  # search index entries disappear immediately
    with db.get_conn() as conn:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    if path.exists():
        path.unlink()
    maintenance.request_vacuum()


def set_file_status(
    file_id: int, status: str, error: str = "", duration: float | None = None, language: str = ""
) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE files SET status = ?, error = ?, "
            "duration = COALESCE(?, duration), "
            "language = CASE WHEN ? != '' THEN ? ELSE language END "
            "WHERE id = ?",
            (status, error, duration, language, language, file_id),
        )
    emit_file_update(file_id)


# ── moving the workspaces root ────────────────────────────────────────


def _target_dir(root: Path, project: dict[str, Any]) -> Path:
    return root / project["slug"]


def move_plan(new_root: Path) -> dict[str, Any]:
    """What a move of the workspaces root would do, without doing it.

    Called before the setting is stored so the user gets a clear refusal
    instead of a half-moved workspace: a name collision in the target
    directory is the one case that cannot be resolved automatically.
    """
    projects = list_projects()
    moves: list[dict[str, str]] = []
    conflicts: list[str] = []
    for project in projects:
        source = Path(project["workspace"])
        target = _target_dir(new_root, project)
        if source == target:
            continue
        if target.exists() and any(target.iterdir()):
            conflicts.append(str(target))
            continue
        moves.append({"slug": project["slug"], "source": str(source), "target": str(target)})
    return {"root": str(new_root), "moves": moves, "conflicts": conflicts}


def move_workspaces(
    new_root: Path,
    cancel: threading.Event | None = None,
    report: Callable[[int, str], None] | None = None,
) -> int:
    """Move every project folder into `new_root` and repoint the database.

    Files are tracked relative to their project folder, so moving the folder
    and rewriting the one absolute path per project is the whole migration —
    audio, transcripts and exports travel with it. A project whose folder has
    gone missing is only repointed (and recreated empty), because there is
    nothing to move.
    """
    new_root.mkdir(parents=True, exist_ok=True)
    plan = move_plan(new_root)
    if plan["conflicts"]:
        raise RuntimeError(
            "Im Zielverzeichnis existieren bereits Ordner mit gleichem Namen: "
            + ", ".join(plan["conflicts"])
        )

    moves = plan["moves"]
    for index, move in enumerate(moves):
        if cancel is not None and cancel.is_set():
            raise JobCancelled()
        if report is not None:
            report(100 * index // max(len(moves), 1), f"Verschiebe {move['slug']} ...")
        source = Path(move["source"])
        target = Path(move["target"])
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():  # empty leftover — move() would nest inside it
                target.rmdir()
            shutil.move(str(source), str(target))
        else:
            logger.warning("workspace folder %s is missing, recreating it at %s", source, target)
            for subdir in WORKSPACE_SUBDIRS:
                (target / subdir).mkdir(parents=True, exist_ok=True)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE projects SET workspace = ? WHERE slug = ?", (str(target), move["slug"])
            )
    if report is not None:
        report(100, f"{len(moves)} Arbeitsbereich(e) verschoben nach {new_root}")
    return len(moves)


def handle_move_workspace_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    """Job handler: move all project folders to the configured root.

    Runs in the main lane, so it never overlaps a transcription that is
    reading from the folders it moves. Payload: {"root": "<absolute path>"}.
    """
    root = job["payload"].get("root", "")
    target = Path(root) if root else config.workspaces_dir(config.get_settings())
    report(0, f"Verschiebe Arbeitsbereiche nach {target} ...")
    move_workspaces(target, cancel, report)
