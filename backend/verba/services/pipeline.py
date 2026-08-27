"""LLM post-processing pipeline: cleanup and translation of transcripts.

Both steps are optional and skipped entirely when no LLM is configured.
Results are stored per file in the derived_texts table and mirrored into the
workspace (transcripts/<stem>.<kind>.md) for user transparency.

Chunking follows segment boundaries with a small overlap so local models with
limited context windows never see a segment cut in half.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import db
from ..core.jobs import JobCancelled, job_queue
from ..events import hub
from . import chunking, llm, transcripts, workspace
from .languages import language_name

CLEANUP_SYSTEM_PROMPT = (
    "You clean up automatic transcriptions. Correct spelling, punctuation, and obvious "
    "mishearings; remove filler words and false starts. Do not change the meaning or "
    "wording beyond that, and do not omit anything. Reply only with the cleaned text, "
    "without commentary."
)

TRANSLATE_SYSTEM_PROMPT = (
    "You translate transcriptions accurately and completely into {language}. Übersetzt "
    "content must preserve "
    "the paragraphs and structure of the original. Reply only with the translation, "
    "without commentary."
)


# ── derived texts ─────────────────────────────────────────────────────


def list_texts(file_id: int) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM derived_texts WHERE file_id = ? ORDER BY kind, language",
            (file_id,),
        ).fetchall()
    return db.rows_to_dicts(rows)


def get_text(file_id: int, kind: str, language: str = "") -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM derived_texts WHERE file_id = ? AND kind = ? AND language = ?",
            (file_id, kind, language),
        ).fetchone()
    return db.row_to_dict(row)


def save_text(file_id: int, kind: str, content: str, language: str = "", model: str = "") -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO derived_texts (file_id, kind, language, content, model) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(file_id, kind, language) DO UPDATE SET "
            "content = excluded.content, model = excluded.model, "
            "created_at = datetime('now')",
            (file_id, kind, language, content, model),
        )
    _write_workspace_copy(file_id, kind, language, content)
    hub.publish("texts.changed", {"file_id": file_id, "kind": kind, "language": language})


def update_text_content(
    file_id: int, kind: str, language: str, content: str
) -> dict[str, Any] | None:
    """User edit of an existing derived text (keeps the model attribution)."""
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE derived_texts SET content = ? WHERE file_id = ? AND kind = ? AND language = ?",
            (content, file_id, kind, language),
        )
        if cursor.rowcount == 0:
            return None
    _write_workspace_copy(file_id, kind, language, content)
    hub.publish("texts.changed", {"file_id": file_id, "kind": kind, "language": language})
    return get_text(file_id, kind, language)


def _write_workspace_copy(file_id: int, kind: str, language: str, content: str) -> None:
    file_row = workspace.get_file(file_id)
    if file_row is None:
        return
    project = workspace.get_project(file_row["project_id"])
    if project is None:
        return
    stem = Path(file_row["rel_path"]).stem
    suffix = f"{kind}.{language}" if language else kind
    target = workspace.project_dir(project) / "transcripts" / f"{stem}.{suffix}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


# ── text chunking for already-joined text (translation input) ─────────


def _chunk_text(text: str, max_chars: int = chunking.DEFAULT_MAX_CHARS) -> list[str]:
    """Split plain text into chunks along paragraph, then line boundaries."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        while len(block) > max_chars:  # oversized paragraph: fall back to lines
            head, _, block = block.partition("\n")
            if not head:
                head, block = block[:max_chars], block[max_chars:]
            if len(current) + len(head) + 1 > max_chars and current:
                parts.append(current)
                current = ""
            current = f"{current}\n{head}" if current else head
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > max_chars and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


# ── automatic chaining after transcription ───────────────────────────


def maybe_enqueue_auto_process(file_id: int, session_id: str = "") -> dict[str, Any] | None:
    """After a finished transcription: enqueue the project's follow-up steps.

    Runs only when the project has auto-processing enabled and an LLM is
    configured; translation is added when the project sets a target language.
    """
    file_row = workspace.get_file(file_id)
    if file_row is None or file_row["status"] != "done":
        return None
    project = workspace.get_project(file_row["project_id"])
    if not project or not project.get("auto_process"):
        return None
    if llm.llm_location() == "none":
        return None

    steps = ["cleanup"]
    target = file_row.get("target_language") or project.get("auto_language") or ""
    if target:
        steps.append("translate")
    return job_queue.enqueue(
        "llm_process",
        payload={"file_id": file_id, "steps": steps, "target_language": target, "model": ""},
        file_id=file_id,
        project_id=file_row["project_id"],
        session_id=session_id,
    )


# ── pipeline steps ────────────────────────────────────────────────────


def cleanup_segments(
    segments: list[dict[str, Any]],
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
) -> str:
    """LLM cleanup of raw segments — also used by the public API."""
    system_prompt = CLEANUP_SYSTEM_PROMPT
    if type_prompt:
        system_prompt += "\n\nKontext zum Transkript:\n" + type_prompt

    chunks = chunking.chunk_segments(segments)
    parts: list[str] = []
    lo, hi = progress_range
    for i, chunk in enumerate(chunks):
        if cancel.is_set():
            raise JobCancelled()
        report(
            lo + (hi - lo) * i // max(1, len(chunks)),
            f"Bereinigung {i + 1}/{len(chunks)}",
        )
        user_content = chunk.own_text
        if chunk.context_text:
            user_content = (
                f"(Kontext des vorherigen Abschnitts, nicht erneut ausgeben: "
                f"{chunk.context_text})\n\n{user_content}"
            )
        parts.append(
            llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                model_override=model_override,
            ).strip()
        )

    return "\n\n".join(parts)


def run_cleanup(
    file_id: int,
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
) -> str:
    segments = transcripts.list_segments(file_id)
    if not segments:
        raise RuntimeError("No segments — transcribe the file first")

    result = cleanup_segments(segments, type_prompt, model_override, cancel, report, progress_range)
    save_text(file_id, "cleanup", result, model=model_override)
    return result


def run_translation(
    file_id: int,
    source_text: str,
    target_language: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
) -> str:
    system_prompt = TRANSLATE_SYSTEM_PROMPT.format(language=language_name(target_language))

    chunks = _chunk_text(source_text)
    parts: list[str] = []
    lo, hi = progress_range
    for i, chunk in enumerate(chunks):
        if cancel.is_set():
            raise JobCancelled()
        report(
            lo + (hi - lo) * i // max(1, len(chunks)),
            f"Übersetzung {i + 1}/{len(chunks)}",
        )
        parts.append(
            llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": chunk},
                ],
                model_override=model_override,
            ).strip()
        )

    result = "\n\n".join(parts)
    save_text(file_id, "translation", result, language=target_language, model=model_override)
    return result


def handle_llm_process_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    """Job handler: run the requested pipeline steps for one file.

    Payload: {"file_id": int, "steps": ["cleanup", "translate"],
              "target_language": "en", "model": ""}
    """
    payload = job["payload"]
    file_id = int(payload["file_id"])
    steps = payload.get("steps") or ["cleanup"]
    model_override = payload.get("model", "")

    file_row = workspace.get_file(file_id)
    if file_row is None:
        raise RuntimeError(f"File {file_id} not found")
    project = workspace.get_project(file_row["project_id"])
    type_prompt = (project or {}).get("type_prompt") or ""

    total_steps = len(steps)
    cleaned: str | None = None

    for step_index, step in enumerate(steps):
        lo = 100 * step_index // total_steps
        hi = 100 * (step_index + 1) // total_steps
        if step == "cleanup":
            cleaned = run_cleanup(file_id, type_prompt, model_override, cancel, report, (lo, hi))
        elif step == "translate":
            target = payload.get("target_language") or "en"
            source = cleaned
            if source is None:
                existing = get_text(file_id, "cleanup")
                source = existing["content"] if existing else None
            if source is None:
                segments = transcripts.list_segments(file_id)
                source = "\n".join(s["text"].strip() for s in segments)
            if not source.strip():
                raise RuntimeError("No text available for translation")
            run_translation(file_id, source, target, model_override, cancel, report, (lo, hi))
        else:
            raise RuntimeError(f"Unknown pipeline step: {step}")

    report(100, "Aufbereitung abgeschlossen")
