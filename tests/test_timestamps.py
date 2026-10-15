"""Timestamps leave the backend as instants, not as forms to interpret.

Every moment in the database is written by `datetime('now')` and sits there as
naive UTC. Nothing about that changes — `sessions.expires_at` is still compared
against `datetime('now')` in SQL — but a row that leaves the backend carries
the marker, so no reader has to know which convention wrote it.
"""

from __future__ import annotations

import datetime

import pytest

from verba import config, db
from verba.services import workspace


@pytest.fixture()
def store(tmp_path):
    """A database and a workspaces root, for the tests that need a real row."""
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()


STAMP = "2026-10-14 14:59:48"
MARKED = "2026-10-14T14:59:48Z"


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def drift(value: str) -> float:
    """How far a marked stamp sits from now, in seconds."""
    moment = datetime.datetime.fromisoformat(value)
    assert moment.tzinfo is not None, f"{value!r} carries no zone"
    return abs((moment - utc_now()).total_seconds())


# ── the marker itself ─────────────────────────────────────────────────


def test_a_stored_instant_is_marked():
    assert db.as_utc(STAMP) == MARKED
    assert datetime.datetime.fromisoformat(db.as_utc(STAMP)) == datetime.datetime(
        2026, 10, 14, 14, 59, 48, tzinfo=datetime.UTC
    )


@pytest.mark.parametrize(
    "value",
    [
        "",  # what a column defaults to while nothing has happened
        None,  # jobs.started_at before the job runs
        "2026-10-14",  # files.recorded_at: the day of a recording, not a moment
        MARKED,  # already marked — never marked twice
        "2026-10-14T16:59:48.123456",  # an older search index, written in local time
        "2026-10-14 14:59",  # not the form `datetime('now')` writes
        "Besprechung 2026-10-14 14:59:48",  # free text that merely contains one
        42,
    ],
)
def test_anything_else_passes_through(value):
    assert db.as_utc(value) == value


# ── the gate every row goes through ───────────────────────────────────


def make_file(tmp_path, recorded_at=""):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("P")
    [file_row] = workspace.import_paths(project, [str(source)])
    if recorded_at:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE files SET recorded_at = ? WHERE id = ?", (recorded_at, file_row["id"])
            )
    return file_row["id"], project


def test_row_to_dict_marks_the_instants_and_keeps_the_calendar_day(store, tmp_path):
    file_id, _ = make_file(tmp_path, recorded_at="2026-10-14")

    file_row = workspace.get_file(file_id)
    assert drift(file_row["created_at"]) < 120
    # ends in `_at` as well, but it is the day a recording was made — the date
    # filter, the sort and the PDF header all read it as a calendar day
    assert file_row["recorded_at"] == "2026-10-14"


def test_rows_to_dicts_marks_every_row(store, tmp_path):
    file_id, project = make_file(tmp_path)

    [listed] = workspace.list_files(project["id"])
    assert listed["id"] == file_id
    assert drift(listed["created_at"]) < 120


def test_an_empty_result_stays_empty():
    assert db.rows_to_dicts([]) == []
    assert db.row_to_dict(None) is None


# ── what the API hands out ────────────────────────────────────────────


def test_api_answers_carry_marked_instants(client):
    project = client.post("/api/projects", json={"name": "P"}).json()
    assert drift(project["created_at"]) < 120

    [listed] = client.get("/api/projects").json()
    assert drift(listed["created_at"]) < 120

    created = client.post("/api/apikeys", json={"name": "CI"}).json()
    assert drift(created["created_at"]) < 120
    # never used yet, and a column that is NULL stays NULL
    assert created["last_used_at"] is None


# ── and what stays on disk ────────────────────────────────────────────


def test_the_marker_never_reaches_the_database(store, tmp_path):
    """The comparisons are string comparisons against `datetime('now')`.

    A "Z" written into the column would sort every marked row after every
    unmarked one, and an expiring session would stop expiring. So the marker
    belongs to the answer, never to the row.
    """
    make_file(tmp_path)
    with db.get_conn() as conn:
        stored = conn.execute("SELECT created_at FROM files").fetchone()["created_at"]
    assert "Z" not in stored and "T" not in stored
    assert datetime.datetime.strptime(stored, "%Y-%m-%d %H:%M:%S")
