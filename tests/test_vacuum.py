"""Database compaction: SQLite reuses freed pages, it never shrinks the file."""

from __future__ import annotations

import os

from verba import db
from verba.core.jobs import job_queue
from verba.services import maintenance


def _fill(rows: int, payload_kb: int = 4) -> None:
    """Enough content to push the file well past the compaction threshold."""
    blob = "x" * (payload_kb * 1024)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects (name, slug, workspace) VALUES ('P','p','/tmp/p')")
        conn.execute("INSERT INTO files (project_id, filename, rel_path) VALUES (1,'a.mp3','a')")
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (1,?,0,1,?)",
            [(i, blob) for i in range(rows)],
        )


def _size() -> int:
    return os.path.getsize(db.db_path())


def test_deleting_leaves_free_space_behind():
    db.init_db()
    _fill(6000)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments")

    stats = db.space_stats()
    assert stats["free"] > db.VACUUM_MIN_BYTES
    assert db.vacuum_worthwhile()


def test_vacuum_shrinks_the_file():
    db.init_db()
    _fill(6000)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments")
    before = _size()

    reclaimed = db.vacuum_if_needed()

    assert reclaimed > 0
    assert _size() < before
    assert db.space_stats()["free"] == 0


def test_a_compact_database_is_left_alone():
    db.init_db()
    _fill(6000)
    assert not db.vacuum_worthwhile()
    assert db.vacuum_if_needed() == 0


def test_the_data_survives_a_vacuum():
    db.init_db()
    _fill(6000)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments WHERE idx > 10")
    db.vacuum()
    with db.get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM segments").fetchone()[0] == 11
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 1


def _queued_vacuums() -> int:
    with db.get_conn() as conn:
        return conn.execute("SELECT count(*) FROM jobs WHERE kind = 'vacuum'").fetchone()[0]


def test_deleting_a_project_queues_a_compaction():
    # no client fixture on purpose: the queue stays stopped, so the job is
    # still sitting there to be asserted on
    from verba.services import workspace

    db.init_db()
    job_queue.register("vacuum", maintenance.handle_vacuum_job)
    project = workspace.create_project("Gross")
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO files (project_id, filename, rel_path) VALUES (?,'a.mp3','a')",
            (project["id"],),
        )
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?,?,0,1,?)",
            [(cursor.lastrowid, i, "x" * 4096) for i in range(6000)],
        )

    workspace.delete_project(project["id"])

    assert job_queue.has_active("vacuum")
    assert _queued_vacuums() == 1
    # a second deletion does not pile another one on top
    maintenance.request_vacuum()
    assert _queued_vacuums() == 1


def test_a_small_deletion_queues_nothing():
    from verba.services import workspace

    db.init_db()
    job_queue.register("vacuum", maintenance.handle_vacuum_job)
    project = workspace.create_project("Klein")
    workspace.delete_project(project["id"])
    assert _queued_vacuums() == 0


def test_the_job_reports_what_it_freed():
    db.init_db()
    _fill(6000)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments")
    messages: list[str] = []

    maintenance.handle_vacuum_job({"payload": {}}, None, lambda p, m="": messages.append(m))

    assert "verdichtet" in messages[-1]
    assert db.space_stats()["free"] == 0
