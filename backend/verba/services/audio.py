"""Audio operations via ffmpeg: range extraction and destructive-safe editing.

Edits never touch the original file — the result is written as a new file in
the workspace and registered as a fresh entry (status pending), ready to be
transcribed.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from ..setup_check import ffmpeg_path
from . import workspace
from .media import probe_duration

logger = logging.getLogger(__name__)

EDIT_OPS = ("trim", "cut")
_EDGE_EPSILON = 0.05  # treat selections touching the file edges as edge-exact


def _ffmpeg() -> str:
    path = ffmpeg_path()
    if path is None:
        raise RuntimeError("ffmpeg is not installed — complete setup first")
    return path


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("ffmpeg error: " + " | ".join(tail))


def extract_range(source: Path, start_s: float, end_s: float, target_wav: Path) -> None:
    """Extract [start_s, end_s] as 16 kHz mono WAV (whisper input format)."""
    _run(
        [
            _ffmpeg(),
            "-y",
            "-ss",
            f"{start_s:.3f}",
            "-to",
            f"{end_s:.3f}",
            "-i",
            str(source),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(target_wav),
        ]
    )


def build_edit_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    op: str,
    start_s: float,
    end_s: float,
    duration: float | None,
) -> list[str]:
    """Build the ffmpeg command for trim/cut (pure function, unit-testable)."""
    if op == "trim":
        return [
            ffmpeg,
            "-y",
            "-ss",
            f"{start_s:.3f}",
            "-to",
            f"{end_s:.3f}",
            "-i",
            str(source),
            str(target),
        ]
    if op == "cut":
        # selections touching an edge degrade to a simple trim of the remainder
        if start_s <= _EDGE_EPSILON:
            return [ffmpeg, "-y", "-ss", f"{end_s:.3f}", "-i", str(source), str(target)]
        if duration is not None and end_s >= duration - _EDGE_EPSILON:
            return [ffmpeg, "-y", "-to", f"{start_s:.3f}", "-i", str(source), str(target)]
        filter_expr = (
            f"[0:a]atrim=end={start_s:.3f},asetpts=N/SR/TB[a];"
            f"[0:a]atrim=start={end_s:.3f},asetpts=N/SR/TB[b];"
            f"[a][b]concat=n=2:v=0:a=1[out]"
        )
        return [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            filter_expr,
            "-map",
            "[out]",
            str(target),
        ]
    raise ValueError(f"Unknown operation: {op}")


def handle_audio_edit_job(job: dict[str, Any], cancel, report) -> None:
    """Job handler: run an audio edit; the result appears as a new pending file."""
    payload = job.get("payload") or {}
    file_row = workspace.get_file(job["file_id"])
    if file_row is None:
        raise RuntimeError(f"File {job['file_id']} no longer exists")
    report(10, f"{file_row['filename']}: {payload['op']} is running ...")
    new_row = apply_edit(
        file_row, payload["op"], float(payload["start_s"]), float(payload["end_s"])
    )
    report(100, f"New file created: {new_row['filename']}")


def apply_edit(file_row: dict[str, Any], op: str, start_s: float, end_s: float) -> dict[str, Any]:
    """Run trim/cut on a workspace file; returns the newly registered file row."""
    if op not in EDIT_OPS:
        raise ValueError(f"Unknown operation: {op}")
    project = workspace.get_project(file_row["project_id"])
    if project is None:
        raise RuntimeError("Transcript no longer exists")

    source = workspace.file_path(file_row)
    audio_dir = workspace.project_dir(project) / "audio"
    stem, suffix = source.stem, source.suffix
    target = workspace.unique_target(audio_dir, f"{stem}-{op}{suffix}")

    duration = file_row.get("duration") or probe_duration(source)
    _run(build_edit_command(_ffmpeg(), source, target, op, start_s, end_s, duration))
    logger.info("Audio edit %s on %s -> %s", op, source.name, target.name)
    return workspace.register_file(project, target, source=str(source))
