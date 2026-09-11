"""Transcript editing: segment CRUD, range merging, workspace JSON sync.

The SQLite `segments` table is the source of truth; after every change the
JSON file in the workspace's transcripts/ folder is rewritten so users always
have an up-to-date, portable copy on disk.

A segment may also carry the moment each of its words was said (`words`),
which the transcription only records where the speaker recognition is going to
need it (services/diarize.py): a segment in which the speaker changes has to
be cut at that change, and without word timings the only place to cut is
somewhere in the middle of a sentence.
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
            "SELECT id, idx, start_s, end_s, text, speaker, words "
            "FROM segments WHERE file_id = ? ORDER BY idx",
            (file_id,),
        ).fetchall()
    return db.rows_to_dicts(rows)


def get_segment(segment_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
    return db.row_to_dict(row)


def create_segment(
    file_id: int,
    start_s: float,
    end_s: float,
    text: str = "",
    speaker: str = "",
) -> dict[str, Any]:
    """Insert one segment and sort it into the transcript by its start time.

    The editor uses this for a passage the recognition missed: a selection on
    the waveform becomes a row one can type into, and the text of a
    re-transcribed selection can be kept instead of only copied. The new row
    is appended behind the highest idx — UNIQUE(file_id, idx) tolerates no
    gap-free insert in place — and `_reindex` then puts it where its start
    time belongs.
    """
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(idx), -1) AS last FROM segments WHERE file_id = ?", (file_id,)
        ).fetchone()
        cursor = conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, row["last"] + 1, start_s, end_s, text, speaker),
        )
        segment_id = int(cursor.lastrowid)
    _reindex(file_id)
    sync_after_change(file_id)
    return get_segment(segment_id)


def update_segment(segment_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
    """Update text/speaker/start_s/end_s/words of one segment; returns the new row.

    `words` is not reachable from the API (no route offers it) — an edited
    text and its old word timings would disagree, and the timings are the
    weaker of the two. Editing the text therefore drops them.
    """
    allowed = {
        k: v for k, v in changes.items() if k in ("text", "speaker", "start_s", "end_s", "words")
    }
    if not allowed:
        return get_segment(segment_id)
    if "text" in allowed and "words" not in allowed:
        allowed["words"] = ""  # the timings described the text that was there
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


#: One word of a transcript: when it started, when it ended, what was said.
Word = tuple[float, float, str]


def decode_words(raw: str) -> list[Word]:
    """The stored word timings, or an empty list for a segment without them.

    Never raises: the column is written by the transcription, but a database
    edited by hand (or by a future version) must not take the editor down.
    """
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        logger.warning("segment word timings are not valid JSON, ignoring them")
        return []
    words: list[Word] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, (list, tuple)) and len(item) == 3:
            try:
                words.append((float(item[0]), float(item[1]), str(item[2])))
            except (TypeError, ValueError):
                continue
    return words


def encode_words(words: list[Word]) -> str:
    """The compact form the column holds — "" for nothing to store."""
    if not words:
        return ""
    return json.dumps(
        [[round(start, 3), round(end, 3), text] for start, end, text in words],
        ensure_ascii=False,
    )


def remap_after_cut(file_id: int, keeps: list[tuple[float, float]]) -> int:
    """Move the segments onto a recording that has just been cut.

    `keeps` are the spans of the *original* recording that survived, so
    `timeline.map_span` says where each segment lands. A segment whose audio is
    gone goes with it — there is no text left to attach to a moment that no
    longer exists. Returns how many were dropped.
    """
    from . import timeline

    dropped: list[int] = []
    moved: list[tuple[float, float, str, int]] = []
    for segment in list_segments(file_id):
        span = timeline.map_span(keeps, segment["start_s"], segment["end_s"])
        if span is None:
            dropped.append(segment["id"])
        else:
            # the word timings name moments in the recording too, so they move
            # with it — a word whose audio is gone goes with its audio
            words = []
            for start, end, text in decode_words(segment.get("words", "")):
                word_span = timeline.map_span(keeps, start, end)
                if word_span is not None:
                    words.append((word_span[0], word_span[1], text))
            moved.append((span[0], span[1], encode_words(words), segment["id"]))
    with db.get_conn() as conn:
        conn.executemany("DELETE FROM segments WHERE id = ?", [(i,) for i in dropped])
        conn.executemany(
            "UPDATE segments SET start_s = ?, end_s = ?, words = ? WHERE id = ?", moved
        )
    _reindex(file_id)
    return len(dropped)


def replace_all_segments(file_id: int, segments: list[dict[str, Any]]) -> None:
    """Put a whole transcript back — the snapshot taken before the first cut."""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments WHERE file_id = ?", (file_id,))
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker, words) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    file_id,
                    index,
                    float(segment["start_s"]),
                    float(segment["end_s"]),
                    segment.get("text", ""),
                    segment.get("speaker", ""),
                    segment.get("words", ""),
                )
                for index, segment in enumerate(segments)
            ],
        )


def apply_speaker_plan(file_id: int, plan: list[dict[str, Any]]) -> int:
    """Write what the speaker recognition worked out; returns how many
    segments it had to cut apart.

    Every entry names one existing segment and the pieces it becomes. One
    piece is the normal case — the segment held one voice and only gains its
    name, so the row keeps its id and the editor keeps its scroll position.
    Several pieces mean the speaker changed inside it: that row goes and the
    pieces take its place, which is the whole point of the exercise
    (services/diarize.py).
    """
    splits = 0
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(idx), -1) AS last FROM segments WHERE file_id = ?", (file_id,)
        ).fetchone()
        next_idx = row["last"] + 1
        for entry in plan:
            pieces = entry["pieces"]
            if len(pieces) == 1:
                conn.execute(
                    "UPDATE segments SET speaker = ? WHERE id = ?",
                    (pieces[0]["speaker"], entry["id"]),
                )
                continue
            splits += 1
            conn.execute("DELETE FROM segments WHERE id = ?", (entry["id"],))
            # appended behind the highest idx and sorted into place by
            # `_reindex` below — UNIQUE(file_id, idx) tolerates no insert in
            # the middle (see create_segment)
            for piece in pieces:
                conn.execute(
                    "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker, words) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        file_id,
                        next_idx,
                        piece["start_s"],
                        piece["end_s"],
                        piece["text"],
                        piece["speaker"],
                        piece.get("words", ""),
                    ),
                )
                next_idx += 1
    _reindex(file_id)
    sync_after_change(file_id)
    return splits


def rename_speaker(file_id: int, old_name: str, new_name: str) -> int:
    """Give one speaker a different name everywhere; returns how often.

    The recognition can only ever say "Sprecher 2", never "Frau Berger". So
    the name it invents is a placeholder, and replacing it one segment at a
    time is exactly the work nobody wants to do on a two-hour interview.
    """
    if not old_name:
        return 0
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE segments SET speaker = ? WHERE file_id = ? AND speaker = ?",
            (new_name, file_id, old_name),
        )
        changed = cursor.rowcount
    if changed:
        sync_after_change(file_id)
    return changed


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
    # What the recording is about was read from these segments; an edited
    # transcript has to be read again before the next document-level step
    # builds on it (imported late: overview belongs to the LLM pipeline).
    from . import overview

    overview.invalidate(file_id)
    hub.publish("segments.changed", {"file_id": file_id}, file_id=file_id)
