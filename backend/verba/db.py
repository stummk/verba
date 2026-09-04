"""SQLite access layer — one database file (<data>/app.db), WAL mode.

Connections are short-lived (one per operation) so worker threads and request
handlers never share a connection. The schema is created idempotently at
startup via init_db().
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Optional user management. The tables exist from the first start on, but
-- stay empty until an admin switches the feature on (settings.auth.enabled):
-- an empty users table means "nobody is logged in, everybody may do anything",
-- which is exactly how the local desktop build is meant to run.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

-- Login sessions. Only the SHA-256 of the cookie value is stored, so a stolen
-- database file does not hand out live sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_types (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    output_prompt TEXT NOT NULL DEFAULT '',
    structure     TEXT NOT NULL DEFAULT 'paragraphs',
    -- 1: a file's section starts on a new page when it would not fit
    -- entirely on the current one (compilation exports)
    keep_sections INTEGER NOT NULL DEFAULT 0,
    builtin       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL UNIQUE,
    workspace     TEXT NOT NULL,
    type_id       INTEGER REFERENCES project_types(id) ON DELETE SET NULL,
    auto_process  INTEGER NOT NULL DEFAULT 0,
    auto_language TEXT NOT NULL DEFAULT '',
    -- Ownership and visibility only mean something while the user management
    -- is switched on. 'public' is the default so a database written before
    -- this existed keeps behaving exactly as it did: everything visible.
    owner_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    visibility    TEXT NOT NULL DEFAULT 'public',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    duration    REAL,
    language    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT '',
    header_left TEXT NOT NULL DEFAULT '',
    header_middle TEXT NOT NULL DEFAULT '',
    header_right TEXT NOT NULL DEFAULT '',
    target_language TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS derived_texts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (file_id, kind, language)
);

CREATE TABLE IF NOT EXISTS segments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,
    start_s  REAL NOT NULL,
    end_s    REAL NOT NULL,
    text     TEXT NOT NULL,
    speaker  TEXT NOT NULL DEFAULT '',
    UNIQUE (file_id, idx)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    file_id     INTEGER,
    project_id  INTEGER,
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    start_s     REAL NOT NULL,
    end_s       REAL NOT NULL,
    text        TEXT NOT NULL,
    speakers    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (file_id, chunk_index)
);

-- Full-text half of the hybrid search; kept in sync by triggers so cascaded
-- deletes (file/project removal) can never leave ghost entries behind.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

-- Which users a 'shared' project is shared with. Both sides cascade, so
-- deleting either the project or the user leaves no dangling grant.
CREATE TABLE IF NOT EXISTS project_shares (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, user_id)
);

-- Keys for the public OpenAI-compatible API; only a SHA-256 hash is stored,
-- the plaintext key is shown exactly once at creation time.
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id);
CREATE INDEX IF NOT EXISTS idx_segments_file ON segments(file_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_derived_texts_file ON derived_texts(file_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


def db_path() -> str:
    return str(config.data_dir() / "app.db")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)


# ── compaction ────────────────────────────────────────────────────────
#
# Deleting a transcript does not shrink the file: SQLite keeps the freed pages
# on a freelist and reuses them for the next insert. That is the right default
# — but after a large deletion (a project with its segments, chunks and
# embeddings) the file can stay several times bigger than its contents, which
# is exactly what someone looking at their backup notices.
#
# VACUUM rewrites the whole file without the free pages, so it costs a full
# read and write of the database and needs the same amount of temporary disk
# space. Both thresholds have to be met before that is worth doing: a fixed
# floor (rewriting a small file to save a megabyte is pointless) and a share of
# the file (a large database with a little slack will refill it anyway).
VACUUM_MIN_BYTES = 16 * 1024 * 1024
VACUUM_MIN_SHARE = 0.2


def space_stats() -> dict[str, int]:
    """How much of the database file is in use, and how much is free space."""
    with get_conn() as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return {"total": page_size * pages, "free": page_size * free_pages}


def vacuum_worthwhile() -> bool:
    stats = space_stats()
    if stats["total"] <= 0:
        return False
    return stats["free"] >= VACUUM_MIN_BYTES and stats["free"] / stats["total"] >= VACUUM_MIN_SHARE


def vacuum() -> int:
    """Rewrite the database without its free pages; returns the bytes reclaimed.

    Own connection in autocommit mode: VACUUM cannot run inside a transaction.
    The checkpoint afterwards truncates the write-ahead log, which would
    otherwise keep the reclaimed space occupied next to the database file.
    """
    path = Path(db_path())
    before = path.stat().st_size if path.exists() else 0
    conn = sqlite3.connect(db_path(), timeout=120, isolation_level=None)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    after = path.stat().st_size if path.exists() else 0
    return max(0, before - after)


def vacuum_if_needed() -> int:
    """Compact the database when enough of it has become free space."""
    return vacuum() if vacuum_worthwhile() else 0


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by older versions."""

    def add_missing(table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    add_missing("segments", "speaker", "speaker TEXT NOT NULL DEFAULT ''")
    add_missing("project_types", "output_prompt", "output_prompt TEXT NOT NULL DEFAULT ''")
    add_missing("project_types", "structure", "structure TEXT NOT NULL DEFAULT 'paragraphs'")
    add_missing("project_types", "keep_sections", "keep_sections INTEGER NOT NULL DEFAULT 0")
    add_missing("projects", "type_id", "type_id INTEGER REFERENCES project_types(id)")
    add_missing("projects", "auto_process", "auto_process INTEGER NOT NULL DEFAULT 0")
    add_missing("projects", "auto_language", "auto_language TEXT NOT NULL DEFAULT ''")
    add_missing("files", "title", "title TEXT NOT NULL DEFAULT ''")
    add_missing("files", "recorded_at", "recorded_at TEXT NOT NULL DEFAULT ''")
    add_missing("files", "header_left", "header_left TEXT NOT NULL DEFAULT ''")
    add_missing("files", "header_middle", "header_middle TEXT NOT NULL DEFAULT ''")
    add_missing("files", "header_right", "header_right TEXT NOT NULL DEFAULT ''")
    add_missing("files", "target_language", "target_language TEXT NOT NULL DEFAULT ''")
    add_missing("projects", "owner_id", "owner_id INTEGER REFERENCES users(id)")
    add_missing("projects", "visibility", "visibility TEXT NOT NULL DEFAULT 'public'")
    # not part of _SCHEMA: that script runs before this migration, so on a
    # database written without the column the index would fail to create
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id)")
    add_missing("jobs", "session_id", "session_id TEXT NOT NULL DEFAULT ''")
    add_missing("jobs", "priority", "priority INTEGER NOT NULL DEFAULT 0")

    if not get_meta(conn, "file_headers_v2_initialized"):
        rows = conn.execute(
            "SELECT id, rel_path, title, recorded_at, header_left, header_middle, header_right "
            "FROM files"
        ).fetchall()
        for row in rows:
            left = row["header_left"] or row["title"] or Path(row["rel_path"]).stem
            right = row["header_right"] or row["recorded_at"]
            if right == row["recorded_at"] and isinstance(right, str) and len(right) == 10:
                right = f"{right[8:10]}.{right[5:7]}.{right[:4]}"
            conn.execute(
                "UPDATE files SET header_left = ?, header_middle = '', header_right = ? "
                "WHERE id = ?",
                (left, right, row["id"]),
            )
        set_meta(conn, "file_headers_v2_initialized", "1")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
