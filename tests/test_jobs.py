from __future__ import annotations

import threading
import time

from verba import db
from verba.core.jobs import JobQueue


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
