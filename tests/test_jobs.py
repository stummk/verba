from __future__ import annotations

import threading
import time

from verba import db
from verba.core.jobs import JobQueue


def test_an_interrupted_cut_is_not_replayed():
    """A cut is the one job that must not run twice.

    Its payload names positions in the recording *as it was*; after a crash
    nobody knows whether it was already written, and running it again would
    take a second, wrong passage out of the file. So it is failed with a
    reason instead of queued again.
    """
    queue = make_queue()
    ran = threading.Event()

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (kind, status, progress) VALUES ('audio_edit', 'running', 60)"
        )

    queue.register("audio_edit", lambda job, cancel, report: ran.set())
    queue.start()
    try:
        assert not ran.wait(0.5)
        with db.get_conn() as conn:
            row = conn.execute("SELECT status, error FROM jobs").fetchone()
        assert row["status"] == "failed"
        assert "Neustart" in row["error"]  # the UI says why, in German
    finally:
        queue.stop()


def test_a_cut_that_never_started_is_still_run():
    """Only a *running* cut is in doubt — a queued one has touched nothing."""
    queue = make_queue()
    ran = threading.Event()

    with db.get_conn() as conn:
        conn.execute("INSERT INTO jobs (kind, status) VALUES ('audio_edit', 'queued')")

    queue.register("audio_edit", lambda job, cancel, report: ran.set())
    queue.start()
    try:
        assert ran.wait(5)
    finally:
        queue.stop()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def make_queue():
    db.init_db()
    return JobQueue()


def test_job_runs_and_completes():
    queue = make_queue()
    done = threading.Event()

    def handler(job, cancel, report):
        report(50, "halbzeit")
        done.set()

    queue.register("demo", handler)
    queue.start()
    try:
        job = queue.enqueue("demo", payload={"x": 1})
        assert done.wait(5)
        assert wait_for(lambda: queue.get(job["id"])["status"] == "done")
        finished = queue.get(job["id"])
        assert finished["progress"] == 100
    finally:
        queue.stop()


def test_handler_result_is_stored_on_the_job():
    """What a handler returns travels with the job — the PDF export names its
    file that way, and the client that started it downloads exactly that one."""
    queue = make_queue()

    def handler(job, cancel, report):
        return "a.en.pdf"

    queue.register("produces", handler)
    queue.start()
    try:
        job = queue.enqueue("produces")
        assert wait_for(lambda: queue.get(job["id"])["status"] == "done")
        assert queue.get(job["id"])["result"] == "a.en.pdf"
    finally:
        queue.stop()


def test_job_without_result_stays_empty():
    queue = make_queue()

    def handler(job, cancel, report):
        pass

    queue.register("quiet", handler)
    queue.start()
    try:
        job = queue.enqueue("quiet")
        assert wait_for(lambda: queue.get(job["id"])["status"] == "done")
        assert queue.get(job["id"])["result"] == ""
    finally:
        queue.stop()


def test_job_failure_is_recorded():
    queue = make_queue()

    def handler(job, cancel, report):
        raise RuntimeError("kaputt")

    queue.register("boom", handler)
    queue.start()
    try:
        job = queue.enqueue("boom")
        assert wait_for(lambda: queue.get(job["id"])["status"] == "failed")
        assert "kaputt" in queue.get(job["id"])["error"]
    finally:
        queue.stop()


def test_running_job_can_be_cancelled():
    queue = make_queue()
    started = threading.Event()

    def handler(job, cancel, report):
        started.set()
        while not cancel.is_set():
            time.sleep(0.02)

    queue.register("slow", handler)
    queue.start()
    try:
        job = queue.enqueue("slow")
        assert started.wait(5)
        assert queue.cancel(job["id"]) is True
        assert wait_for(lambda: queue.get(job["id"])["status"] == "cancelled")
    finally:
        queue.stop()


def test_queued_job_can_be_cancelled_before_start():
    queue = make_queue()  # not started: jobs stay queued

    queue.register("later", lambda job, cancel, report: None)
    job = queue.enqueue("later")
    assert queue.cancel(job["id"]) is True
    assert queue.get(job["id"])["status"] == "cancelled"


def test_unknown_kind_is_rejected():
    queue = make_queue()
    try:
        queue.enqueue("gibtsnicht")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_interrupted_jobs_are_requeued():
    queue = make_queue()
    executed = threading.Event()

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (kind, status, progress) VALUES ('resume-me', 'running', 40)"
        )

    queue.register("resume-me", lambda job, cancel, report: executed.set())
    queue.start()
    try:
        assert executed.wait(5)
    finally:
        queue.stop()
