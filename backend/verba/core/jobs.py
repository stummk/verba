"""Persistent job queue with lane-based scheduling.

Jobs are stored in SQLite (survives restarts) and executed by two workers:

- lane "main": transcription and audio edits (GPU/CPU-bound, strictly one at
  a time so local hardware is never oversubscribed — multi-user safe)
- lane "llm":  LLM post-processing. Whether it runs in parallel depends on
  where the LLM lives (services.llm.llm_location):
    remote → runs alongside transcription (two lanes, local hardware is
             only used by Whisper while the remote API cleans up texts)
    local  → phased batching: LLM jobs wait until the main lane is idle,
             then the Whisper model is unloaded once and all LLM jobs run
             as a batch — both models never fight over VRAM/RAM.

Fairness: within a lane, jobs are picked by priority (small interactive jobs
first), then round-robin across sessions (FIFO per session), so one user's
bulk import cannot starve everyone else.

Handlers are registered per job kind:

    def handle(job: dict, cancel: threading.Event, report: Callable[[int, str], None]) -> None

A handler that raises JobCancelled (or returns after `cancel` is set) marks
the job as cancelled; any other exception marks it as failed.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from .. import db
from ..events import hub

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], threading.Event, Callable[[int, str], None]], None]

# export_pdf may call the LLM for structuring, so it shares the llm lane
# (phased batching with a local model, parallel with a remote one).
LLM_KINDS = {"llm_process", "export_pdf"}
# Small interactive jobs jump ahead of long-running batch transcriptions.
DEFAULT_PRIORITIES = {"transcribe_range": 1, "audio_edit": 1}

LANES = ("main", "llm")

# The UI has to say *which file* a step is running on, so every job row is read
# with its file name attached instead of leaving the frontend to look it up.
JOB_SELECT = "SELECT j.*, f.filename FROM jobs j LEFT JOIN files f ON f.id = j.file_id"


def broadcast_job(job: dict[str, Any]) -> None:
    """A job event names a file, so it only reaches clients who may see it."""
    hub.publish("job.update", job, project_id=job.get("project_id"), file_id=job.get("file_id"))


def lane_for_kind(kind: str) -> str:
    return "llm" if kind in LLM_KINDS else "main"


class JobCancelled(Exception):
    pass


class JobQueue:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._running: dict[str, int | None] = {lane: None for lane in LANES}
        self._last_picked: dict[str, dict[str, int]] = {lane: {} for lane in LANES}
        self._pick_counter = 0
        self._cond = threading.Condition()
        self._stopping = False

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        with self._cond:
            self._stopping = False
            self._requeue_interrupted()
            for lane in LANES:
                worker = self._workers.get(lane)
                if worker is not None and worker.is_alive():
                    continue
                worker = threading.Thread(
                    target=self._run, args=(lane,), daemon=True, name=f"job-worker-{lane}"
                )
                self._workers[lane] = worker
                worker.start()

    def stop(self) -> None:
        with self._cond:
            if not self._workers:
                return
            self._stopping = True
            self._cond.notify_all()
        for worker in self._workers.values():
            worker.join(timeout=5)
        self._workers.clear()

    def _requeue_interrupted(self) -> None:
        """Jobs left running by a previous process get queued again."""
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'queued', progress = 0, message = '' "
                "WHERE status IN ('queued', 'running')"
            )

    # ── API ──────────────────────────────────────────────────────────

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        file_id: int | None = None,
        project_id: int | None = None,
        session_id: str = "",
        priority: int | None = None,
    ) -> dict[str, Any]:
        if kind not in self._handlers:
            raise ValueError(f"Unknown job type: {kind}")
        if priority is None:
            priority = DEFAULT_PRIORITIES.get(kind, 0)
        with db.get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs (kind, payload, file_id, project_id, session_id, priority) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (kind, json.dumps(payload or {}), file_id, project_id, session_id, priority),
            )
            job_id = cursor.lastrowid
        job = self.get(job_id)
        assert job is not None
        broadcast_job(job)
        with self._cond:
            self._cond.notify_all()
        return job

    def cancel(self, job_id: int) -> bool:
        job = self.get(job_id)
        if job is None or job["status"] not in ("queued", "running"):
            return False
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()  # running: the handler notices and aborts
        else:
            self._finish(job_id, "cancelled")
        return True

    def get(self, job_id: int) -> dict[str, Any] | None:
        with db.get_conn() as conn:
            row = conn.execute(f"{JOB_SELECT} WHERE j.id = ?", (job_id,)).fetchone()
        return db.row_to_dict(row)

    def active_jobs_for_file(self, kind: str, file_id: int) -> list[dict[str, Any]]:
        """Every queued or running job of that kind for the file, in queue order.

        Callers compare the payloads to see which work is already on its way —
        a step whose progress is easy to miss gets clicked again, while a step
        nobody has started yet still has to be enqueued.
        """
        with db.get_conn() as conn:
            rows = conn.execute(
                f"{JOB_SELECT} WHERE j.kind = ? AND j.file_id = ? "
                "AND j.status IN ('queued', 'running') ORDER BY j.id ASC",
                (kind, file_id),
            ).fetchall()
        jobs = db.rows_to_dicts(rows)
        for job in jobs:
            if isinstance(job.get("payload"), str):
                job["payload"] = json.loads(job["payload"] or "{}")
        return jobs

    def list_jobs(self, active_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        query = JOB_SELECT
        if active_only:
            query += " WHERE j.status IN ('queued', 'running')"
        query += " ORDER BY j.id DESC LIMIT ?"
        with db.get_conn() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return db.rows_to_dicts(rows)

    def queue_overview(self) -> dict[str, Any]:
        """Queued/running jobs in pick order with their queue position (for the UI)."""
        overview: dict[str, Any] = {"llm_location": self._llm_location(), "lanes": {}}
        with db.get_conn() as conn:
            for lane in LANES:
                rows = conn.execute(
                    f"{JOB_SELECT} WHERE j.status IN ('queued', 'running') "
                    "ORDER BY (j.status = 'running') DESC, j.priority DESC, j.id ASC"
                ).fetchall()
                jobs = [dict(r) for r in rows if lane_for_kind(r["kind"]) == lane]
                for position, job in enumerate(jobs):
                    job["queue_position"] = position
                    job.pop("payload", None)
                overview["lanes"][lane] = jobs
        return overview

    # ── scheduling ───────────────────────────────────────────────────

    @staticmethod
    def _llm_location() -> str:
        from ..services.llm import llm_location

        try:
            return llm_location()
        except Exception:  # settings problems must never kill the scheduler
            return "none"

    def _main_lane_busy(self) -> bool:
        """Running or queued main-lane work blocks local-LLM batching."""
        if self._running["main"] is not None:
            return True
        with db.get_conn() as conn:
            rows = conn.execute("SELECT kind FROM jobs WHERE status = 'queued'").fetchall()
        return any(lane_for_kind(r["kind"]) == "main" for r in rows)

    def _pick_next(self, lane: str) -> int | None:
        """Next queued job for a lane: priority, then session round-robin FIFO."""
        if lane == "llm" and self._llm_location() == "local" and self._main_lane_busy():
            return None  # phased batching: wait until transcription is done

        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, kind, session_id, priority FROM jobs "
                "WHERE status = 'queued' ORDER BY priority DESC, id ASC"
            ).fetchall()
        candidates = [r for r in rows if lane_for_kind(r["kind"]) == lane]
        if not candidates:
            return None

        top_priority = candidates[0]["priority"]
        top = [r for r in candidates if r["priority"] == top_priority]
        last_picked = self._last_picked[lane]
        # earliest job of the session that has waited longest since its last pick
        per_session: dict[str, Any] = {}
        for row in top:
            per_session.setdefault(row["session_id"], row)
        chosen = min(
            per_session.items(), key=lambda item: (last_picked.get(item[0], 0), item[1]["id"])
        )[1]
        self._pick_counter += 1
        last_picked[chosen["session_id"]] = self._pick_counter
        return chosen["id"]

    # ── worker ───────────────────────────────────────────────────────

    def _run(self, lane: str) -> None:
        while True:
            with self._cond:
                job_id = None
                while not self._stopping:
                    job_id = self._pick_next(lane)
                    if job_id is not None:
                        break
                    # timeout so the llm lane re-checks once the main lane drains
                    self._cond.wait(timeout=2.0)
                if self._stopping:
                    return
                self._running[lane] = job_id
            try:
                self._execute(job_id)  # type: ignore[arg-type]
            except Exception:
                logger.exception("job worker (%s): unexpected error in job %s", lane, job_id)
            finally:
                with self._cond:
                    self._running[lane] = None
                    self._cond.notify_all()

    def _execute(self, job_id: int) -> None:
        job = self.get(job_id)
        if job is None or job["status"] != "queued":
            return  # cancelled or deleted while waiting

        handler = self._handlers.get(job["kind"])
        if handler is None:
            self._finish(job_id, "failed", error=f"No handler for job type '{job['kind']}'")
            return

        cancel = threading.Event()
        self._cancel_events[job_id] = cancel
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
        self._publish(job_id)

        job["payload"] = json.loads(job["payload"] or "{}")

        def report(percent: int, message: str = "") -> None:
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE jobs SET progress = ?, message = ? WHERE id = ?",
                    (max(0, min(100, percent)), message, job_id),
                )
            self._publish(job_id)

        try:
            self._prepare_resources(job)
            handler(job, cancel, report)
            self._finish(job_id, "cancelled" if cancel.is_set() else "done")
        except JobCancelled:
            self._finish(job_id, "cancelled")
        except Exception as exc:
            logger.exception("job %s (%s) failed", job_id, job["kind"])
            self._finish(job_id, "failed", error=str(exc))
        finally:
            self._cancel_events.pop(job_id, None)

    def _prepare_resources(self, job: dict[str, Any]) -> None:
        """Local LLM and Whisper share the hardware — swap models, not thrash them."""
        if self._llm_location() != "local":
            return
        from ..services import llamacpp, whisper

        if lane_for_kind(job["kind"]) == "llm":
            whisper.unload_model()  # free VRAM/RAM before the LLM loads
        else:
            llamacpp.stop_server()  # transcription batch begins: LLM releases memory

    def _finish(self, job_id: int, status: str, error: str = "") -> None:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = datetime('now'), "
                "progress = CASE WHEN ? = 'done' THEN 100 ELSE progress END WHERE id = ?",
                (status, error, status, job_id),
            )
        self._publish(job_id)

    def _publish(self, job_id: int) -> None:
        job = self.get(job_id)
        if job is not None:
            broadcast_job(job)


job_queue = JobQueue()
