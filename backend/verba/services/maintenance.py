"""Housekeeping on the database file.

Deleting transcripts frees pages inside `app.db` without making the file any
smaller — see `db.vacuum_if_needed()` for why, and for the thresholds that
decide when a rewrite pays off. This module is the part that decides *when*
that runs: a background job, so a delete request returns immediately and the
compaction does not block the request that caused it.

The job runs in the main lane, where it cannot overlap a transcription, and it
re-checks the thresholds before doing anything — several deletions in a row
therefore collapse into a single rewrite instead of one per deletion.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .. import db
from ..core.jobs import job_queue

logger = logging.getLogger(__name__)


def request_vacuum(session_id: str = "") -> None:
    """Queue a compaction if enough of the database has become free space."""
    if not db.vacuum_worthwhile() or job_queue.has_active("vacuum"):
        return
    job_queue.enqueue("vacuum", session_id=session_id)


def handle_vacuum_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    """Job handler: compact the database file.

    No progress in between — VACUUM is a single statement and holds the
    database while it runs, so a progress write would only queue up behind it.
    """
    report(0, "Datenbank wird verdichtet ...")
    reclaimed = db.vacuum_if_needed()
    if not reclaimed:
        report(100, "Datenbank ist bereits kompakt")
        return
    megabytes = reclaimed / (1024 * 1024)
    logger.info("database compacted, %.1f MiB reclaimed", megabytes)
    report(100, f"Datenbank verdichtet, {megabytes:.0f} MB freigegeben")
