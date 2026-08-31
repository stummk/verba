"""Upgrading an existing installation must cost nothing.

The database of a version that never knew about users is opened, migrated and
used further: the transcripts stay, stay reachable, and only become owned once
somebody actually switches the user management on.
"""

from __future__ import annotations

import sqlite3

import pytest

from verba import config, db
from verba.services import auth, workspace

# Exactly the tables a pre-user-management build created, with the columns it
# had — no users, no sessions, no owner_id, no visibility.
LEGACY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE project_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    output_prompt TEXT NOT NULL DEFAULT '',
    structure TEXT NOT NULL DEFAULT 'paragraphs',
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL,
    type_id INTEGER,
    auto_process INTEGER NOT NULL DEFAULT 0,
    auto_language TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    duration REAL,
    language TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT '',
    header_left TEXT NOT NULL DEFAULT '',
    header_middle TEXT NOT NULL DEFAULT '',
    header_right TEXT NOT NULL DEFAULT '',
    target_language TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (file_id, idx)
);
"""


@pytest.fixture()
def legacy_db(tmp_path):
    """A database file written by the previous version, with real content."""
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)

    conn = sqlite3.connect(db.db_path())
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO projects (id, name, slug, workspace) "
        "VALUES (1, 'Altbestand', 'altbestand', ?)",
        (str(tmp_path / "workspaces" / "altbestand"),),
    )
    conn.execute(
        "INSERT INTO files (id, project_id, filename, rel_path, status) "
        "VALUES (1, 1, 'lied.mp3', 'audio/lied.mp3', 'done')"
    )
    conn.execute(
        "INSERT INTO segments (file_id, idx, start_s, end_s, text) "
        "VALUES (1, 0, 0.0, 2.0, 'Erster Satz')"
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_the_new_columns_are_added_to_an_existing_database(legacy_db):
    db.init_db()

    with db.get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert {"owner_id", "visibility"} <= columns
    assert {"users", "sessions", "project_shares"} <= tables


def test_nothing_is_lost_and_everything_stays_reachable(legacy_db):
    db.init_db()

    project = workspace.get_project(1)

    assert project["name"] == "Altbestand"
    assert project["visibility"] == "public"  # exactly as reachable as before
    assert project["owner_id"] is None
    assert len(workspace.list_files(1)) == 1
    # no user in the request while the management is off, and still everything
    assert [p["id"] for p in workspace.list_projects(None)] == [1]


def test_the_upgraded_installation_still_answers_without_a_login(legacy_db, client):
    assert client.get("/api/projects").json()[0]["name"] == "Altbestand"
    assert client.get("/api/auth/state").json()["enabled"] is False


def test_switching_the_protection_on_afterwards_keeps_the_old_transcript(legacy_db, client):
    client.post("/api/auth/enable", json={"username": "chef", "password": "geheim1234"})

    project = client.get("/api/projects/1").json()

    assert project["name"] == "Altbestand"
    assert project["visibility"] == "public"
    assert project["owner_id"] == auth.get_user_by_name("chef")["id"]
    assert len(client.get("/api/files/1/segments").json()["segments"]) == 1


def test_migrating_twice_changes_nothing(legacy_db):
    db.init_db()
    workspace.create_project("Danach")
    db.init_db()

    assert {p["name"] for p in workspace.list_projects()} == {"Danach", "Altbestand"}
    assert workspace.get_project(1)["visibility"] == "public"
