"""Semantic search index: one global hybrid index across all projects.

Chunks follow segment boundaries (small, search-sized, with overlap) and keep
their timestamps, so every hit can jump straight into the editor at the right
audio position. Two halves per chunk:

- full text in an FTS5 table (kept in sync by DB triggers — proper names and
  rare words match even when embeddings miss them)
- an embedding in a sqlite-vec table (multilingual model, CPU-friendly)

Beside the transcript text, a file's header (title, the three header fields,
date, file name) is searched directly in SQL: a name, a date or an extra note
lives there, not in the spoken text, and an exact lookup beats a semantic one
for those. Results are fused with reciprocal rank fusion. Each chunk records the
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
from pathlib import Path
from typing import Any

from .. import config, db
from ..core.jobs import JobCancelled, job_queue
from ..events import hub
from . import chunking, hardware, transcripts, workspace

logger = logging.getLogger(__name__)

# Search chunks are much smaller than LLM chunks: a hit should be one
# readable passage, not a page.
CHUNK_MAX_CHARS = 800
CHUNK_OVERLAP_SEGMENTS = 1

# Header fields of a file: the name, date and extra note a transcript carries
# around its text. They are searched literally (every token has to appear),
# which is what a lookup for "Meier 2024" expects.
HEADER_FIELDS = (
    "title",
    "header_left",
    "header_middle",
    "header_right",
    "recorded_at",
    "filename",
)
MAX_HEADER_TOKENS = 8
# a header lookup answers "which file is this?" — a handful is plenty
MAX_HEADER_HITS = 5

VEC_DIM_KEY = "search_vec_dim"
LAST_INDEX_KEY = "search_last_index"

_model_lock = threading.Lock()
_model: Any = None
# name plus embeddings directory: pointing the directory somewhere else has to
# reload the model, not keep serving the one from the old path
_model_key: tuple[str, str] | None = None


def available() -> bool:
    return find_spec("sqlite_vec") is not None and find_spec("sentence_transformers") is not None


class EmbeddingUnavailable(RuntimeError):
    """The configured embedding model could not be loaded (or downloaded)."""


# ── embedding model ───────────────────────────────────────────────────


def local_model_dir(entry: config.EmbeddingModel) -> Path | None:
    """A folder in the embeddings directory that already holds this model.

    Two layouts count, because both turn up in practice: a plain folder (the
    repo copied or cloned by hand, `bge-m3/` or `BAAI_bge-m3/`) and the
    HuggingFace cache layout (`models--BAAI--bge-m3/snapshots/<rev>/`). A hit
    is loaded from disk, so the model is never downloaded twice.
    """
    root = config.embeddings_dir()
    org, _, repo = entry.name.partition("/")
    plain = [
        root / repo,
        root / entry.name.replace("/", "_"),
        root / entry.name.replace("/", "--"),
    ]
    for candidate in plain:
        if (candidate / "config.json").is_file():
            return candidate
    cache = root / f"models--{org}--{repo}" / "snapshots"
    if cache.is_dir():
        for snapshot in sorted(cache.iterdir(), reverse=True):
            if (snapshot / "config.json").is_file():
                return snapshot
    return None


def model_present_locally(entry: config.EmbeddingModel) -> bool:
    """Whether using this model needs a download first (settings UI)."""
    return local_model_dir(entry) is not None


def _load_model() -> Any:
    """Lazy-load the configured sentence-transformers model (CPU).

    The model is chosen from a curated catalog (config.EMBEDDING_MODELS). One
    that already lies in the embeddings directory is loaded from there; only a
    genuinely missing one is downloaded — the one moment this can fail without
    a network, so the error says as much.
    """
    global _model, _model_key
    entry = config.embedding_model(config.get_settings().search.embedding_model)
    directory = config.embeddings_dir()
    key = (entry.name, str(directory))
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        verdict = hardware.check_embedding_model(entry.size_mb)
        if verdict["level"] == hardware.NO:
            # refused before torch allocates: an impossible allocation would
            # take the process down, not just this index run
            hub.publish(
                "engine.status",
                {"engine": "embeddings", "state": "error", "detail": verdict["message"]},
            )
            raise EmbeddingUnavailable(
                f"Das Embedding-Modell „{entry.label}“ passt nicht in den Speicher. "
                f"{verdict['message']}"
            )
        hub.publish(
            "engine.status",
            {"engine": "embeddings", "state": "loading", "detail": entry.name},
        )
        from sentence_transformers import SentenceTransformer

        local = local_model_dir(entry)
        try:
            _model = SentenceTransformer(
                str(local) if local else entry.name,
                cache_folder=str(directory),
                device="cpu",
            )
        except Exception as exc:  # noqa: BLE001 — network, disk and model errors alike
            hub.publish("engine.status", {"engine": "embeddings", "state": "error", "detail": ""})
            logger.exception("embedding model %s could not be loaded", entry.name)
            if hardware.is_oom(exc):
                raise EmbeddingUnavailable(
                    f"{hardware.oom_message('cpu', name=entry.label)} "
                    f"Bitte ein kleineres Embedding-Modell wählen."
                ) from exc
            raise EmbeddingUnavailable(
                f"Das Embedding-Modell „{entry.label}“ konnte nicht geladen werden "
                f"(wird beim ersten Gebrauch heruntergeladen, ca. {entry.size_mb} MB): {exc}"
            ) from exc
        _model_key = key
        hub.publish("engine.status", {"engine": "embeddings", "state": "idle", "detail": ""})
        return _model


def unload_model() -> None:
    global _model, _model_key
    with _model_lock:
        _model = None
        _model_key = None
    hardware.invalidate_probe()  # the freed memory counts for the next check


def _encode(texts: list[str], kind: str = "passage") -> list[list[float]]:
    """Embed texts; `kind` picks the model's query or passage prefix.

    The E5 family is trained with those prefixes and loses noticeable quality
    without them, while the sbert models define none — hence the catalog
    carries them per model instead of hard-coding one convention.
    """
    entry = config.embedding_model(config.get_settings().search.embedding_model)
    prefix = entry.query_prefix if kind == "query" else entry.passage_prefix
    model = _load_model()
    prepared = [f"{prefix}{text}" for text in texts] if prefix else texts
    return [list(map(float, row)) for row in model.encode(prepared, normalize_embeddings=True)]


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
    embeddings = _encode([r["text"] for r in rows], "passage") if rows else []
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

    query_vector = _encode([query], "query")[0]
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
        results = _load_results(conn, fused, filters, limit) if fused else []
        # header matches are exact and few: they lead, the passages follow
        headers = _header_hits(conn, query, filters, min(limit, MAX_HEADER_HITS))
        return headers + results


def group_by_file(results: list[dict]) -> list[dict]:
    """One entry per file with all its hits — the shape the hit list needs.

    A file that matches several times is one result with a list of positions,
    not the same file over and over. Group order follows relevance (a file's
    best hit decides), the hits inside a file follow the timeline.
    """
    groups: dict[int, dict[str, Any]] = {}
    for hit in results:
        group = groups.get(hit["file_id"])
        if group is None:
            group = {
                "file_id": hit["file_id"],
                "filename": hit["filename"],
                "title": hit["title"],
                "project_id": hit["project_id"],
                "project_name": hit["project_name"],
                "language": hit["language"],
                "recorded_at": hit["recorded_at"],
                "header": header_line(hit),
                "hits": [],
            }
            groups[hit["file_id"]] = group
        group["hits"].append(
            {
                "chunk_id": hit["id"],
                "start_s": hit["start_s"],
                "end_s": hit["end_s"],
                "text": hit["text"],
                "speakers": hit["speakers"],
                "source": hit.get("source", "transcript"),
            }
        )
    for group in groups.values():
        group["hits"].sort(key=lambda hit: (hit["source"] != "header", hit["start_s"]))
    return list(groups.values())


FILE_COLUMNS = (
    "f.filename, f.title, f.recorded_at, f.language, f.project_id, "
    "f.header_left, f.header_middle, f.header_right, "
    "p.name AS project_name, p.type_id"
)


def _filter_clause(filters: dict[str, Any], speaker_sql: str) -> tuple[str, list[Any]]:
    """The shared file/project filters; `speaker_sql` differs per hit source.

    The visibility of the transcript is one of them, and not an optional one:
    the search runs across every project at once, so this is the place where a
    private transcript would otherwise show up in somebody else's hit list.
    """
    from .auth import visibility_clause  # local import: auth imports the db layer

    sql = ""
    params: list[Any] = []
    visible, visible_params = visibility_clause(filters.get("user"))
    if visible:
        sql += f" AND {visible}"
        params.extend(visible_params)
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
        sql += f" AND {speaker_sql}"
        params.append(f"%{filters['speaker']}%")
    if filters.get("date_from"):
        sql += " AND f.recorded_at >= ?"
        params.append(filters["date_from"])
    if filters.get("date_to"):
        sql += " AND f.recorded_at <= ?"
        params.append(filters["date_to"])
    return sql, params


def _load_results(
    conn: sqlite3.Connection, chunk_ids: list[int], filters: dict[str, Any], limit: int
) -> list[dict]:
    marks = ",".join("?" for _ in chunk_ids)
    sql = (
        "SELECT c.id, c.file_id, c.start_s, c.end_s, c.text, c.speakers, "
        f"{FILE_COLUMNS} "
        f"FROM chunks c JOIN files f ON f.id = c.file_id "
        f"JOIN projects p ON p.id = f.project_id WHERE c.id IN ({marks})"
    )
    params: list[Any] = list(chunk_ids)
    clause, filter_params = _filter_clause(filters, "c.speakers LIKE ?")
    sql += clause
    params += filter_params

    by_id = {row["id"]: dict(row) | {"source": "transcript"} for row in conn.execute(sql, params)}
    return [by_id[cid] for cid in chunk_ids if cid in by_id][:limit]


# ── header search ─────────────────────────────────────────────────────


def _header_tokens(query: str) -> list[str]:
    """The query tokens a header lookup is run with.

    Every token has to appear, so a whole question simply finds nothing here
    instead of dragging unrelated files in. Very short words are dropped —
    they would match inside longer names — except numbers, which carry the
    parts of a date ("12.05.2024").
    """
    tokens = []
    for token in re.findall(r"\w+", query.lower(), re.UNICODE):
        if len(token) >= 3 or (token.isdigit() and len(token) >= 2):
            tokens.append(token)
    return tokens[:MAX_HEADER_TOKENS]


def header_line(row: dict[str, Any]) -> str:
    """The header as one readable line (what the UI shows under a file)."""
    parts = [
        str(row.get(field) or "").strip()
        for field in ("header_left", "header_middle", "header_right")
    ]
    return " · ".join(part for part in parts if part)


def _sql_lower(value: str | None) -> str:
    """Unicode-aware lowercase for the header match (SQL helper)."""
    return (value or "").lower()


def _header_hits(
    conn: sqlite3.Connection, query: str, filters: dict[str, Any], limit: int
) -> list[dict]:
    """Files whose header (name, date, note, title) matches the query itself."""
    tokens = _header_tokens(query)
    if not tokens or limit <= 0:
        return []
    # SQLite's own LOWER() only folds ASCII, which would make "MUELLER" match
    # but "MULLER" (with an umlaut) or a Cyrillic name not
    conn.create_function("verba_lower", 1, _sql_lower, deterministic=True)
    haystack = " || ' ' || ".join(f"COALESCE(f.{field}, '')" for field in HEADER_FIELDS)
    conditions = " AND ".join(f"INSTR(verba_lower({haystack}), ?) > 0" for _ in tokens)
    clause, filter_params = _filter_clause(
        filters,
        "EXISTS (SELECT 1 FROM chunks c WHERE c.file_id = f.id AND c.speakers LIKE ?)",
    )
    sql = (
        f"SELECT f.id AS file_id, {FILE_COLUMNS} "
        "FROM files f JOIN projects p ON p.id = f.project_id "
        f"WHERE f.status = 'done' AND ({conditions}){clause} "
        "ORDER BY f.recorded_at DESC, f.id DESC LIMIT ?"
    )
    params = [*tokens, *filter_params, limit]
    hits = []
    for row in conn.execute(sql, params):
        hit = dict(row)
        title = (hit["title"] or hit["filename"]).strip()
        date = (hit["recorded_at"] or "").strip()
        header = header_line(hit)
        hit |= {
            "id": None,
            "start_s": 0.0,
            "end_s": 0.0,
            "speakers": "",
            "source": "header",
            "text": " · ".join(part for part in (title, date, header) if part),
        }
        hits.append(hit)
    return hits


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
    entry = config.embedding_model(config.get_settings().search.embedding_model)
    return {
        "available": available(),
        "files_indexed": row["files"],
        "chunk_count": row["chunks"],
        "index_models": models,
        "configured_model": entry.name,
        "configured_label": entry.label,
        # a model change invalidates every vector: the UI offers a reindex
        "model_mismatch": any(model != entry.name for model in models),
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
    report(10, "Indiziere für die Suche ...")
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
        report(
            100 * index // max(1, len(file_ids)),
            f"Datei {index + 1}/{len(file_ids)}",
        )
        total_chunks += index_file(file_id)
    report(100, f"Neu indiziert: {len(file_ids)} Datei(en), {total_chunks} Abschnitte")
