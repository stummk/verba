"""Audio operations via ffmpeg: extracting a passage, and cutting a recording.

Cutting works the way an audio editor works: the editor collects the cuts
without touching anything, and one "apply" turns the whole collection into a
single ffmpeg pass that rewrites the file **in place**. There is no new file per
cut — the point of cutting is to change this recording, and a workspace full of
`name-cut-cut-trim.mp3` was the old way of saying that.

Two things make that safe enough to do to somebody's recording:

- Before the first cut, the untouched file is copied aside once
  (`audio/.original/`), together with the segments as they were, so the whole
  editing can be undone later (`restore_original`). One copy per file, not per
  cut, and it can be switched off (`general.audio_backup`).
- The transcript follows the audio: `services.timeline` says where every
  position of the original ends up, segments are moved with it, and the ones
  whose audio is gone are deleted.

The cut is written to a temporary file next to the original and moved over it
with `os.replace`, so an ffmpeg that dies half-way leaves the recording alone.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any

from .. import config, db, procutil
from ..setup_check import ffmpeg_path
from . import timeline, transcripts, workspace
from .media import probe_duration

logger = logging.getLogger(__name__)

ORIGINAL_DIR = ".original"  # inside the transcript's audio folder


def _ffmpeg() -> str:
    path = ffmpeg_path()
    if path is None:
        raise RuntimeError("ffmpeg fehlt — bitte die Einrichtung abschließen")
    return path


def _run(cmd: list[str]) -> None:
    result = procutil.run(cmd, capture_output=True, text=True, timeout=1800)
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


# ── the cut itself ────────────────────────────────────────────────────


def build_keep_command(
    ffmpeg: str, source: Path, target: Path, keeps: list[timeline.Span]
) -> list[str]:
    """The one ffmpeg pass that reduces a recording to `keeps` (pure function).

    A single span is a plain trim — `-ss/-to` in front of the input, which lets
    ffmpeg seek instead of reading the whole file. Several spans need the filter
    graph: one `atrim` per span, concatenated in order.
    """
    if not keeps:
        raise ValueError("A recording cannot be cut down to nothing")
    if len(keeps) == 1:
        start, end = keeps[0]
        return [
            ffmpeg, "-y",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", str(source),
            str(target),
        ]  # fmt: skip

    parts = []
    for index, (start, end) in enumerate(keeps):
        parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=N/SR/TB[p{index}];")
    inputs = "".join(f"[p{index}]" for index in range(len(keeps)))
    filter_expr = "".join(parts) + f"{inputs}concat=n={len(keeps)}:v=0:a=1[out]"
    return [
        ffmpeg, "-y",
        "-i", str(source),
        "-filter_complex", filter_expr,
        "-map", "[out]",
        str(target),
    ]  # fmt: skip


def _original_paths(file_row: dict[str, Any]) -> tuple[Path, Path]:
    """Where the untouched audio and the segments as they were are kept."""
    source = workspace.file_path(file_row)
    folder = source.parent / ORIGINAL_DIR
    return folder / source.name, folder / f"{source.name}.segments.json"


def has_original(file_row: dict[str, Any]) -> bool:
    """Whether this recording can be brought back to how it was imported."""
    if not file_row.get("audio_original"):
        return False
    audio, _segments = _original_paths(file_row)
    return audio.exists()


def remove_original(file_row: dict[str, Any]) -> None:
    """Throw the kept original away — the one place that says "no backup".

    Used from three sides: after a restore has put it back, as the rollback of
    a cut that did not get written, and when the file itself is deleted. It is
    a full copy of the recording, so leaving it behind would double the
    workspace for every recording that was ever cut.
    """
    audio, segments_file = _original_paths(file_row)
    audio.unlink(missing_ok=True)
    segments_file.unlink(missing_ok=True)
    with suppress(OSError):
        audio.parent.rmdir()  # empty once the last backup of the folder is gone
    with db.get_conn() as conn:
        conn.execute("UPDATE files SET audio_original = '' WHERE id = ?", (file_row["id"],))


def _keep_original(file_row: dict[str, Any]) -> bool:
    """Copy the untouched recording aside — once, before the first cut.

    Returns whether *this* call created it, so a cut that fails afterwards can
    take it back without touching a backup an earlier cut had made.
    """
    if not config.get_settings().general.audio_backup or has_original(file_row):
        return False
    audio, segments_file = _original_paths(file_row)
    audio.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(workspace.file_path(file_row), audio)
    # the transcript belongs to the audio it was made from: cutting moves the
    # timestamps and drops segments, and neither can be reconstructed
    segments_file.write_text(
        json.dumps(
            {
                "duration": file_row.get("duration"),
                "segments": [
                    {
                        "start_s": row["start_s"],
                        "end_s": row["end_s"],
                        "text": row["text"],
                        "speaker": row["speaker"],
                    }
                    for row in transcripts.list_segments(file_row["id"])
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE files SET audio_original = ? WHERE id = ?",
            (f"{ORIGINAL_DIR}/{audio.name}", file_row["id"]),
        )
    logger.info("Kept the untouched recording of file %s at %s", file_row["id"], audio)
    return True


def apply_keeps(
    file_row: dict[str, Any], keeps: list[timeline.Span], report=None
) -> dict[str, Any]:
    """Cut a workspace recording down to `keeps`, in place. Returns the file row.

    Everything that hangs off the audio follows: the duration, the segments and
    their JSON copy in the workspace, and the search index.
    """
    source = workspace.file_path(file_row)
    if not source.exists():
        raise RuntimeError(f"Audio file is missing: {source}")
    duration = file_row.get("duration") or probe_duration(source) or 0.0
    keeps = timeline.normalize(keeps, duration or None)
    if not keeps:
        raise ValueError("A recording cannot be cut down to nothing")
    if timeline.covers_all(keeps, duration or None):
        return file_row  # the cuts cancelled out — nothing to rewrite

    # Order matters: cut into a temporary file, then put the untouched
    # recording aside, and only then replace it. A cut that fails — no ffmpeg,
    # a broken container — thus leaves neither a changed recording nor a
    # backup offering to "restore" a file that was never touched.
    if report:
        report(20, f"{file_row['filename']}: schneide ...")
    target = source.with_name(f"{source.stem}.cut-tmp{source.suffix}")
    kept_now = False
    try:
        _run(build_keep_command(_ffmpeg(), source, target, keeps))
        if report:
            report(60, f"{file_row['filename']}: sichere das Original ...")
        kept_now = _keep_original(file_row)
        os.replace(target, source)  # only now does the recording change
    except Exception:
        # The recording is as it was, so a backup this call made would claim an
        # edit that never happened — and offer to "restore" from it. Only the
        # one made here is taken back; a backup from an earlier, successful cut
        # is still the untouched original.
        if kept_now:
            remove_original(file_row)
        raise
    finally:
        target.unlink(missing_ok=True)

    if report:
        report(75, f"{file_row['filename']}: rechne die Segmente um ...")
    removed = transcripts.remap_after_cut(file_row["id"], keeps)
    new_duration = probe_duration(source) or timeline.total(keeps)
    workspace.set_file_status(file_row["id"], file_row["status"], duration=new_duration)
    transcripts.sync_after_change(file_row["id"])
    workspace.emit_file_update(file_row["id"])

    from .vectorstore import maybe_enqueue_index

    maybe_enqueue_index(file_row["id"])  # every timestamp in the index moved
    logger.info(
        "Cut %s down to %.1fs (%d spans, %d segments dropped)",
        source.name,
        new_duration,
        len(keeps),
        removed,
    )
    updated = workspace.get_file(file_row["id"])
    assert updated is not None
    return updated


def restore_original(file_row: dict[str, Any], report=None) -> dict[str, Any]:
    """Put the untouched recording — and the transcript it belonged to — back."""
    audio, segments_file = _original_paths(file_row)
    if not file_row.get("audio_original") or not audio.exists():
        raise RuntimeError("Von dieser Aufnahme liegt keine Sicherung vor")
    source = workspace.file_path(file_row)
    if report:
        report(20, f"{file_row['filename']}: stelle das Original wieder her ...")
    shutil.copy2(audio, source)

    snapshot: dict[str, Any] = {}
    if segments_file.exists():
        try:
            snapshot = json.loads(segments_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Could not read the segment snapshot at %s", segments_file)
    duration = snapshot.get("duration") or probe_duration(source) or 0.0
    if "segments" in snapshot:
        if report:
            report(70, f"{file_row['filename']}: stelle die Segmente wieder her ...")
        transcripts.replace_all_segments(file_row["id"], snapshot["segments"])

    remove_original(file_row)  # it is the working file again, not a backup
    workspace.set_file_status(file_row["id"], file_row["status"], duration=duration)
    transcripts.sync_after_change(file_row["id"])
    workspace.emit_file_update(file_row["id"])

    from .vectorstore import maybe_enqueue_index

    maybe_enqueue_index(file_row["id"])
    logger.info("Restored the untouched recording of file %s", file_row["id"])
    updated = workspace.get_file(file_row["id"])
    assert updated is not None
    return updated


# ── job handlers ──────────────────────────────────────────────────────


def handle_audio_edit_job(job: dict[str, Any], cancel, report) -> None:
    """Job handler: rewrite a recording so that only `keeps` is left of it."""
    payload = job.get("payload") or {}
    file_row = workspace.get_file(job["file_id"])
    if file_row is None:
        raise RuntimeError(f"File {job['file_id']} no longer exists")
    keeps = [(float(start), float(end)) for start, end in payload.get("keeps") or []]
    updated = apply_keeps(file_row, keeps, report)
    report(100, f"{updated['filename']}: geschnitten ({_minutes(updated.get('duration'))})")


def handle_audio_restore_job(job: dict[str, Any], cancel, report) -> None:
    """Job handler: undo every cut ever applied to a recording."""
    file_row = workspace.get_file(job["file_id"])
    if file_row is None:
        raise RuntimeError(f"File {job['file_id']} no longer exists")
    updated = restore_original(file_row, report)
    report(100, f"{updated['filename']}: Original wiederhergestellt")


def _minutes(seconds: float | None) -> str:
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}"
