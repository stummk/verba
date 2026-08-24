"""Semantic search index: one global hybrid index across all projects.

Chunks follow segment boundaries (small, search-sized, with overlap) and keep
their timestamps, so every hit can jump straight into the editor at the right
audio position. Two halves per chunk:

- full text in an FTS5 table (kept in sync by DB triggers — proper names and
  rare words match even when embeddings miss them)
- an embedding in a sqlite-vec table (multilingual model, CPU-friendly)

Results are fused with reciprocal rank fusion. Each chunk records the
embedding model it was built with; changing the model in the settings
triggers a full reindex job. Both heavy imports (sentence-transformers,
sqlite-vec) belong to the optional "search" feature group — every entry
point checks availability first.
"""

from __future__ import annotations

import datetime
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.util import find_spec
from typing import Any

from .. import config, db
from ..core.jobs import JobCancelled, job_queue
from ..events import hub
from . import chunking, transcripts, workspace

logger = logging.getLogger(__name__)

# Search chunks are much smaller than LLM chunks: a hit should be one
# readable passage, not a page.
CHUNK_MAX_CHARS = 800
CHUNK_OVERLAP_SEGMENTS = 1

VEC_DIM_KEY = "search_vec_dim"
LAST_INDEX_KEY = "search_last_index"

_model_lock = threading.Lock()
_model: Any = None
_model_name = ""


def available() -> bool:
    return find_spec("sqlite_vec") is not None and find_spec("sentence_transformers") is not None


# ── embedding model ───────────────────────────────────────────────────


def _load_model() -> Any:
    """Lazy-load the configured sentence-transformers model (CPU)."""
    global _model, _model_name
    name = config.get_settings().search.embedding_model
    with _model_lock:
        if _model is not None and _model_name == name:
            return _model
        hub.publish(
            "engine.status",
            {"engine": "embeddings", "state": "loading", "detail": name},
        )
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(name, cache_folder=str(config.embeddings_dir()), device="cpu")
        _model_name = name
        hub.publish("engine.status", {"engine": "embeddings", "state": "idle", "detail": ""})
        return _model


def unload_model() -> None:
    global _model, _model_name
    with _model_lock:
        _model = None
        _model_name = ""


def _encode(texts: list[str]) -> list[list[float]]:
    model = _load_model()
    return [list(map(float, row)) for row in model.encode(texts, normalize_embeddings=True)]


# ── vec connection & table ────────────────────────────────────────────


@contextmanager
def _vec_conn() -> Iterator[sqlite3.Connection]:
    """DB connection with the sqlite-vec extension loaded."""
    import sqlite_vec

    conn = sqlite3.connect(db.db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _vec_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vec_chunks'"
    ).fetchone()
    return row is not None


def _ensure_vec_table(conn: sqlite3.Connection, dim: int) -> None:
    stored = db.get_meta(conn, VEC_DIM_KEY)
    if _vec_table_exists(conn) and stored == str(dim):
        return
    conn.execute("DROP TABLE IF EXISTS vec_chunks")
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_chunks USING vec0(chunk_id INTEGER PRIMARY KEY, "
        f"embedding FLOAT[{dim}])"
    )
    db.set_meta(conn, VEC_DIM_KEY, str(dim))


def _serialize(vector: list[float]) -> bytes:
    import sqlite_vec

    return sqlite_vec.serialize_float32(vector)


# ── indexing ──────────────────────────────────────────────────────────


def _chunk_rows(file_id: int) -> list[dict[str, Any]]:
    segments = transcripts.list_segments(file_id)
    rows = []
    for index, chunk in enumerate(
        chunking.chunk_segments(segments, max_chars=CHUNK_MAX_CHARS, overlap=CHUNK_OVERLAP_SEGMENTS)
    ):
        own = chunk.segments[chunk.own_start :]
        speakers = sorted({s["speaker"].strip() for s in own if s.get("speaker", "").strip()})
        rows.append(
            {
                "chunk_index": index,
                "start_s": own[0]["start_s"],
                "end_s": own[-1]["end_s"],
                "text": chunk.own_text,
                "speakers": ", ".join(speakers),
            }
        )
    return rows


def index_file(file_id: int) -> int:
    """(Re-)index one file; returns the number of chunks written."""
    rows = _chunk_rows(file_id)
    embeddings = _encode([r["text"] for r in rows]) if rows else []
    model_name = config.get_settings().search.embedding_model

    with _vec_conn() as conn:
        if embeddings:
            _ensure_vec_table(conn, len(embeddings[0]))
        _delete_file_chunks(conn, file_id)
        for row, embedding in zip(rows, embeddings, strict=True):
            cursor = conn.execute(
                "INSERT INTO chunks (file_id, chunk_index, start_s, end_s, text, speakers, model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    row["chunk_index"],
                    row["start_s"],
                    row["end_s"],
                    row["text"],
                    row["speakers"],
                    model_name,
                ),
            )
            if _vec_table_exists(conn):
                conn.execute(
                    "INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)",
                    (cursor.lastrowid, _serialize(embedding)),
                )
        db.set_meta(conn, LAST_INDEX_KEY, datetime.datetime.now().isoformat(timespec="seconds"))
    return len(rows)


def _delete_file_chunks(conn: sqlite3.Connection, file_id: int) -> None:
    ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))]
    if ids and _vec_table_exists(conn):
        marks = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM vec_chunks WHERE chunk_id IN ({marks})", ids)
    conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))


def remove_file(file_id: int) -> None:
    """Drop a file's index entries immediately (called on file deletion)."""
    if find_spec("sqlite_vec") is not None:
        with _vec_conn() as conn:
            _delete_file_chunks(conn, file_id)
    else:  # without the extension there is no vec table — chunks + FTS suffice
        with db.get_conn() as conn:
            conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))


def remove_project(project_id: int) -> None:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT id FROM files WHERE project_id = ?", (project_id,))
        file_ids = [r["id"] for r in rows]
    for file_id in file_ids:
        remove_file(file_id)


# ── hybrid search ─────────────────────────────────────────────────────


def _fts_query(query: str) -> str:
    tokens = re.findall(r"\w+", query, re.UNICODE)
    return " OR ".join(f'"{token}"' for token in tokens)


def _rrf(*ranked_lists: list[int], k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda cid: scores[cid], reverse=True)


def search(query: str, filters: dict[str, Any] | None = None, limit: int = 10) -> list[dict]:
    """Hybrid search (vector + full text), fused via reciprocal rank fusion."""
    filters = filters or {}
    candidates = max(40, limit * 4)

    query_vector = _encode([query])[0]
    with _vec_conn() as conn:
        vec_ids: list[int] = []
        if _vec_table_exists(conn):
            vec_ids = [
                row["chunk_id"]
                for row in conn.execute(
                    "SELECT chunk_id FROM vec_chunks WHERE embedding MATCH ? "
                    "ORDER BY distance LIMIT ?",
                    (_serialize(query_vector), candidates),
                )
            ]
        fts = _fts_query(query)
        fts_ids: list[int] = []
        if fts:
            fts_ids = [
                row["rowid"]
                for row in conn.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "ORDER BY bm25(chunks_fts) LIMIT ?",
                    (fts, candidates),
                )
            ]
        fused = _rrf(vec_ids, fts_ids)
        if not fused:
            return []
        return _load_results(conn, fused, filters, limit)


def _load_results(
    conn: sqlite3.Connection, chunk_ids: list[int], filters: dict[str, Any], limit: int
) -> list[dict]:
    marks = ",".join("?" for _ in chunk_ids)
    sql = (
        "SELECT c.id, c.file_id, c.start_s, c.end_s, c.text, c.speakers, "
        "f.filename, f.title, f.recorded_at, f.language, f.project_id, "
        "p.name AS project_name, p.type_id "
        f"FROM chunks c JOIN files f ON f.id = c.file_id "
        f"JOIN projects p ON p.id = f.project_id WHERE c.id IN ({marks})"
    )
    params: list[Any] = list(chunk_ids)
    if filters.get("project_id"):
        sql += " AND f.project_id = ?"
        params.append(filters["project_id"])
    if filters.get("type_id"):
        sql += " AND p.type_id = ?"
        params.append(filters["type_id"])
    if filters.get("language"):
        sql += " AND f.language = ?"
        params.append(filters["language"])
    if filters.get("speaker"):
        sql += " AND c.speakers LIKE ?"
        params.append(f"%{filters['speaker']}%")
    if filters.get("date_from"):
        sql += " AND f.recorded_at >= ?"
        params.append(filters["date_from"])
    if filters.get("date_to"):
        sql += " AND f.recorded_at <= ?"
        params.append(filters["date_to"])

    by_id = {row["id"]: dict(row) for row in conn.execute(sql, params)}
    return [by_id[cid] for cid in chunk_ids if cid in by_id][:limit]


# ── status ────────────────────────────────────────────────────────────


def status() -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS chunks, COUNT(DISTINCT file_id) AS files FROM chunks"
        ).fetchone()
        models = [
            r["model"] for r in conn.execute("SELECT DISTINCT model FROM chunks WHERE model != ''")
        ]
        last_index = db.get_meta(conn, LAST_INDEX_KEY)
    return {
        "available": available(),
        "files_indexed": row["files"],
        "chunk_count": row["chunks"],
        "index_models": models,
        "configured_model": config.get_settings().search.embedding_model,
        "last_index": last_index,
    }


# ── jobs & automatic consistency ──────────────────────────────────────


def _has_queued_job(kind: str, file_id: int | None) -> bool:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE kind = ? AND status = 'queued' "
            "AND (file_id = ? OR (? IS NULL)) LIMIT 1",
            (kind, file_id, file_id),
        ).fetchone()
    return row is not None


def maybe_enqueue_index(file_id: int, session_id: str = "") -> dict[str, Any] | None:
    """Index a file in the background (after transcription or segment edits)."""
    if not available():
        return None
    file_row = workspace.get_file(file_id)
    if file_row is None or file_row["status"] != "done":
        return None
    if _has_queued_job("index_file", file_id):
        return None
    return job_queue.enqueue(
        "index_file",
        payload={"file_id": file_id},
        file_id=file_id,
        project_id=file_row["project_id"],
        session_id=session_id,
    )


def enqueue_reindex(session_id: str = "") -> dict[str, Any] | None:
    if _has_queued_job("reindex_search", None):
        return None
    return job_queue.enqueue("reindex_search", payload={}, session_id=session_id)


def handle_index_file_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    file_id = int(job["payload"]["file_id"])
    report(10, "Indexing for search ...")
    count = index_file(file_id)
    report(100, f"{count} Abschnitte indiziert")


def handle_reindex_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    """Full rebuild — used after an embedding-model change."""
    with _vec_conn() as conn:
        conn.execute("DELETE FROM chunks")
        conn.execute("DROP TABLE IF EXISTS vec_chunks")
        conn.execute("DELETE FROM meta WHERE key = ?", (VEC_DIM_KEY,))
    unload_model()

    with db.get_conn() as conn:
        file_ids = [r["id"] for r in conn.execute("SELECT id FROM files WHERE status = 'done'")]
    total_chunks = 0
    for index, file_id in enumerate(file_ids):
        if cancel.is_set():
            raise JobCancelled()
        report(100 * index // max(1, len(file_ids)), f"File {index + 1}/{len(file_ids)}")
        total_chunks += index_file(file_id)
    report(100, f"Reindexed: {len(file_ids)} file(s), {total_chunks} chunks")
