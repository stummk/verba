"""Scheduling rules of the two-lane JobQueue (no worker threads involved:
_pick_next is exercised directly, so the tests stay timing-free)."""

from __future__ import annotations

import threading
import time

from verba import db
from verba.core.jobs import JobQueue, lane_for_kind


def make_queue() -> JobQueue:
    db.init_db()
    queue = JobQueue()
    for kind in ("transcribe", "transcribe_range", "audio_edit", "llm_process"):
        queue.register(kind, lambda job, cancel, report: None)
    return queue


def drain(queue: JobQueue, lane: str) -> list[int]:
    """Simulate the worker's pick order without executing anything."""
    order = []
    while True:
        job_id = queue._pick_next(lane)
        if job_id is None:
            return order
        order.append(job_id)
        with db.get_conn() as conn:
            conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))


def test_lane_mapping():
    assert lane_for_kind("transcribe") == "main"
    assert lane_for_kind("audio_edit") == "main"
    assert lane_for_kind("llm_process") == "llm"


def test_small_jobs_jump_the_queue():
    queue = make_queue()
    big = queue.enqueue("transcribe")
    small = queue.enqueue("transcribe_range")  # default priority 1
    assert drain(queue, "main") == [small["id"], big["id"]]


def test_fifo_round_robin_across_sessions():
    queue = make_queue()
    a1 = queue.enqueue("transcribe", session_id="alice")
    a2 = queue.enqueue("transcribe", session_id="alice")
    a3 = queue.enqueue("transcribe", session_id="alice")
    b1 = queue.enqueue("transcribe", session_id="bob")

    order = drain(queue, "main")
    # bob's single job must not wait behind alice's whole batch
    assert order.index(b1["id"]) < order.index(a2["id"])
    # per session the order stays FIFO
    assert order.index(a1["id"]) < order.index(a2["id"]) < order.index(a3["id"])


def test_local_llm_jobs_wait_for_main_lane(monkeypatch):
    queue = make_queue()
    monkeypatch.setattr(JobQueue, "_llm_location", staticmethod(lambda: "local"))

    transcribe = queue.enqueue("transcribe")
    llm_job = queue.enqueue("llm_process", payload={"file_id": 1})

    # while transcription is queued, the llm lane yields nothing
    assert queue._pick_next("llm") is None

    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (transcribe["id"],))
    assert queue._pick_next("llm") == llm_job["id"]


def test_remote_llm_jobs_run_immediately(monkeypatch):
    queue = make_queue()
    monkeypatch.setattr(JobQueue, "_llm_location", staticmethod(lambda: "remote"))

    queue.enqueue("transcribe")
    llm_job = queue.enqueue("llm_process", payload={"file_id": 1})
    assert queue._pick_next("llm") == llm_job["id"]


def test_lanes_run_in_parallel_with_remote_llm(monkeypatch):
    monkeypatch.setattr(JobQueue, "_llm_location", staticmethod(lambda: "remote"))
    db.init_db()
    queue = JobQueue()
    both_running = threading.Event()
    running = set()
    lock = threading.Lock()

    def tracking_handler(job, cancel, report):
        with lock:
            running.add(job["kind"])
            if {"transcribe", "llm_process"} <= running:
                both_running.set()
        both_running.wait(3)  # hold until overlap is proven (or timeout)
        with lock:
            running.discard(job["kind"])

    queue.register("transcribe", tracking_handler)
    queue.register("llm_process", tracking_handler)
    queue.start()
    try:
        queue.enqueue("transcribe")
        queue.enqueue("llm_process", payload={"file_id": 1})
        assert both_running.wait(5), "main and llm lane never ran at the same time"
    finally:
        queue.stop()


def test_queue_overview_positions():
    queue = make_queue()
    first = queue.enqueue("transcribe", session_id="s1")
    second = queue.enqueue("transcribe", session_id="s1")

    overview = queue.queue_overview()
    main = overview["lanes"]["main"]
    assert [j["id"] for j in main] == [first["id"], second["id"]]
    assert [j["queue_position"] for j in main] == [0, 1]
    assert overview["llm_location"] == "none"


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_local_llm_batch_runs_after_transcriptions(monkeypatch):
    """End-to-end: with a local LLM the llm job only starts once main drained."""
    monkeypatch.setattr(JobQueue, "_llm_location", staticmethod(lambda: "none"))
    db.init_db()
    queue = JobQueue()
    order: list[str] = []

    def slow_transcribe(job, cancel, report):
        time.sleep(0.1)
        order.append("transcribe")

    def llm_handler(job, cancel, report):
        order.append("llm")

    queue.register("transcribe", slow_transcribe)
    queue.register("llm_process", llm_handler)

    monkeypatch.setattr(JobQueue, "_llm_location", staticmethod(lambda: "local"))
    monkeypatch.setattr(JobQueue, "_prepare_resources", lambda self, job: None)
    queue.start()
    try:
        queue.enqueue("transcribe")
        queue.enqueue("llm_process", payload={"file_id": 1})
        queue.enqueue("transcribe")
        assert wait_for(lambda: len(order) == 3)
        assert order == ["transcribe", "transcribe", "llm"]
    finally:
        queue.stop()
