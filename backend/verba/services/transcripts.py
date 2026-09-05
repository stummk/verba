"""Transcript editing: segment CRUD, range merging, workspace JSON sync.

The SQLite `segments` table is the source of truth; after every change the
JSON file in the workspace's transcripts/ folder is rewritten so users always
have an up-to-date, portable copy on disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .. import db
from ..events import hub
from . import workspace

logger = logging.getLogger(__name__)


def list_segments(file_id: int) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, idx, start_s, end_s, text, speaker "
            "FROM segments WHERE file_id = ? ORDER BY idx",
            (file_id,),
        ).fetchall()
    return db.rows_to_dicts(rows)


def get_segment(segment_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
    return db.row_to_dict(row)


def update_segment(segment_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Update text/speaker/start_s/end_s of one segment; returns the new row."""
    allowed = {k: v for k, v in changes.items() if k in ("text", "speaker", "start_s", "end_s")}
    if not allowed:
        return get_segment(segment_id)
    sets = ", ".join(f"{column} = ?" for column in allowed)
    with db.get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE segments SET {sets} WHERE id = ?",  # noqa: S608 — columns whitelisted
            (*allowed.values(), segment_id),
        )
        if cursor.rowcount == 0:
            return None
    segment = get_segment(segment_id)
    if segment is not None:
        sync_after_change(segment["file_id"])
    return segment


def delete_segment(segment_id: int) -> bool:
    segment = get_segment(segment_id)
    if segment is None:
        return False
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
    _reindex(segment["file_id"])
    sync_after_change(segment["file_id"])
    return True


def _reindex(file_id: int) -> None:
    """Renumber idx by start time (stable, gap-free)."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM segments WHERE file_id = ? ORDER BY start_s, idx", (file_id,)
        ).fetchall()
        # negative pass avoids UNIQUE(file_id, idx) collisions while shifting
        conn.executemany(
            "UPDATE segments SET idx = ? WHERE id = ?",
            [(-(i + 1), row["id"]) for i, row in enumerate(rows)],
        )
        conn.executemany(
            "UPDATE segments SET idx = ? WHERE id = ?",
            [(i, row["id"]) for i, row in enumerate(rows)],
        )


def write_transcript_json(file_id: int) -> None:
    """Rewrite the portable JSON copy in <workspace>/transcripts/."""
    file_row = workspace.get_file(file_id)
    if file_row is None:
        return
    project = workspace.get_project(file_row["project_id"])
    if project is None:
        return
    out_dir = workspace.project_dir(project) / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / (Path(file_row["filename"]).stem + ".json")
    segments = [
        {
            "start": seg["start_s"],
            "end": seg["end_s"],
            "text": seg["text"],
            "speaker": seg["speaker"],
        }
        for seg in list_segments(file_id)
    ]
    out_file.write_text(
        json.dumps(
            {
                "file": file_row["filename"],
                "language": file_row["language"],
                "duration": file_row["duration"],
                "segments": segments,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def sync_after_change(file_id: int) -> None:
    write_transcript_json(file_id)
    hub.publish("segments.changed", {"file_id": file_id}, file_id=file_id)
