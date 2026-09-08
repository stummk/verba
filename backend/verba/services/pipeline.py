"""LLM post-processing pipeline: cleanup and translation of transcripts.

Both steps are optional and skipped entirely when no LLM is configured.
Results are stored per file in the derived_texts table and mirrored into the
workspace (transcripts/<stem>.<kind>.md) for user transparency.

Chunking follows segment boundaries with a small overlap so local models with
limited context windows never see a segment cut in half. What a chunk cannot
know is the recording it belongs to, and that matters in two different ways
(`overview.py` supplies both from one bounded reading of the whole text):

- A step that reproduces its material — cleanup, translation — stays
  chunk-local, because every sentence has to come back. It only takes the
  overview as orientation, so names, terms and spellings stay the same across
  chunk boundaries.
- A transcript type that turns its material into something else
  (`project_types.verbatim` off: minutes, a summary) must not run per chunk at
  all — the same instruction would produce one document per chunk, four
  titles for a file that was split into four. Its instruction runs once, over
  the digests of the whole recording.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import db
from ..core.jobs import JobCancelled, job_queue, report_step, split_range
from ..events import hub
from . import chunking, llm, overview, project_types, transcripts, workspace
from .languages import language_name

logger = logging.getLogger(__name__)

CLEANUP_SYSTEM_PROMPT = (
    "You clean up automatic transcriptions. Correct spelling, punctuation, and obvious "
    "mishearings; remove filler words and false starts. Do not change the meaning or "
    "wording beyond that, and do not omit anything. Reply only with the cleaned text, "
    "without commentary."
)

# The register is part of the meaning: a sermon that comes back in everyday
# phrasing ("so sagt der Heilige Geist" for "so spricht der Heilige Geist") is
# not wrong word by word and still reads as the wrong text. So the translation
# is asked to carry the register and the subject matter over, and it is given
# the same type context the cleanup gets.
TRANSLATE_SYSTEM_PROMPT = (
    "You translate transcriptions accurately and completely into {language}. Carry the "
    "register and the subject matter of the original over: a sermon, a prophecy or a "
    "liturgical text is translated in the established biblical wording of {language}, a "
    "technical, legal or scholarly text in the settled terminology of its field — never "
    "in everyday paraphrase. Where the original uses a fixed expression of its domain, "
    "use the fixed expression the target language has for it, and keep names, quotations "
    "and references in the form that domain uses. Preserve the paragraphs and structure "
    "of the original. Reply only with the translation, without commentary."
)

# both steps hang the type's own prompt off the same heading
TYPE_CONTEXT_HEADING = "\n\nKontext zum Transkript:\n"


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
    _publish_change(file_id, kind, language)


def delete_text(file_id: int, kind: str, language: str = "") -> bool:
    """Drop a derived text for good — the row and its workspace copy."""
    with db.get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM derived_texts WHERE file_id = ? AND kind = ? AND language = ?",
            (file_id, kind, language),
        )
        if cursor.rowcount == 0:
            return False
    _remove_workspace_copy(file_id, kind, language)
    _publish_change(file_id, kind, language)
    return True


def _publish_change(file_id: int, kind: str, language: str) -> None:
    # The search indexes the derived texts alongside the transcript, so every
    # write here is a change to what is findable — a name the cleanup corrected
    # is only searchable once this ran. Imported late: the vectorstore builds
    # its chunks from this module.
    from .vectorstore import maybe_enqueue_index

    maybe_enqueue_index(file_id)

    # The kinds travel with the event: an emptied or deleted text has to take
    # the "cleaned"/"translated" chip off the file row, and the receiver cannot
    # tell from one language alone whether the kind is gone for the whole file.
    hub.publish(
        "texts.changed",
        {
            "file_id": file_id,
            "kind": kind,
            "language": language,
            "kinds": present_kinds(file_id),
        },
        file_id=file_id,
    )


def present_kinds(file_id: int) -> str:
    """The file's non-empty derived kinds, comma separated as in FILE_SELECT."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT kind FROM derived_texts "
            "WHERE file_id = ? AND trim(content) != '' ORDER BY kind",
            (file_id,),
        ).fetchall()
    return ",".join(row["kind"] for row in rows)


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
    _publish_change(file_id, kind, language)
    return get_text(file_id, kind, language)


def _workspace_copy_path(file_id: int, kind: str, language: str) -> Path | None:
    file_row = workspace.get_file(file_id)
    if file_row is None:
        return None
    project = workspace.get_project(file_row["project_id"])
    if project is None:
        return None
    stem = Path(file_row["rel_path"]).stem
    suffix = f"{kind}.{language}" if language else kind
    return workspace.project_dir(project) / "transcripts" / f"{stem}.{suffix}.md"


def _write_workspace_copy(file_id: int, kind: str, language: str, content: str) -> None:
    target = _workspace_copy_path(file_id, kind, language)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _remove_workspace_copy(file_id: int, kind: str, language: str) -> None:
    target = _workspace_copy_path(file_id, kind, language)
    if target is not None:
        target.unlink(missing_ok=True)


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


# ── one piece through the LLM, complete ──────────────────────────────

# Below this a cut-off answer is no longer a size problem — splitting further
# would only shred the text without ever getting a complete answer.
MIN_SPLIT_CHARS = 800


class SizeLimit:
    """How much this endpoint managed to answer completely, for one run.

    Once a model has shown that a 6000-character piece is more than it can
    answer, every following piece is split up front. Without that memory a
    two-hour recording would pay for a cut-off answer on every single chunk.
    """

    def __init__(self) -> None:
        self.max_chars = 0  # 0: nothing learned yet, send the piece as it is

    def too_big(self, text: str) -> bool:
        return bool(self.max_chars) and len(text) > self.max_chars

    def shrink_to(self, chars: int) -> None:
        learned = max(MIN_SPLIT_CHARS, chars)
        self.max_chars = min(self.max_chars, learned) if self.max_chars else learned


def chat_pieces(
    system_prompt: str,
    user_text: str,
    model_override: str = "",
    context: str = "",
    limit: SizeLimit | None = None,
) -> list[str]:
    """Answers for one piece of text — more than one when it had to be split.

    No token cap is sent, so the endpoint answers as far as its context
    allows. Should the answer still be cut off, the piece is split and sent
    again instead of storing a shortened transcript: a two-hour recording has
    to come back complete, whatever context window the model brings. Pass the
    same `limit` for all pieces of one run so the split size is learned once.
    """
    limit = limit if limit is not None else SizeLimit()
    if not limit.too_big(user_text):
        content = user_text
        if context:
            content = (
                f"(Kontext des vorherigen Abschnitts, nicht erneut ausgeben: "
                f"{context})\n\n{content}"
            )
        try:
            return [
                llm.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": content},
                    ],
                    model_override=model_override,
                ).strip()
            ]
        except llm.TruncatedAnswer:
            if len(user_text) <= MIN_SPLIT_CHARS:
                raise  # even a short piece does not fit: the model is too small
            limit.shrink_to(len(user_text) // 2)

    pieces: list[str] = []
    for index, part in enumerate(_chunk_text(user_text, max_chars=limit.max_chars)):
        # the context belongs to the first part only — the others follow their
        # own predecessor, which the model has just seen
        pieces.extend(
            chat_pieces(system_prompt, part, model_override, context if index == 0 else "", limit)
        )
    return pieces


# ── which steps are already on their way ─────────────────────────────

DEFAULT_TARGET_LANGUAGE = "en"


def step_key(step: str, target_language: str = "") -> str:
    """Identity of one pipeline step for a file.

    A translation is one step *per language*, so a running English translation
    must not swallow the request for a Russian one.
    """
    if step != "translate":
        return step
    return f"translate:{target_language or DEFAULT_TARGET_LANGUAGE}"


def active_steps(file_id: int) -> dict[str, dict[str, Any]]:
    """Step keys already queued or running for the file → the job doing them."""
    taken: dict[str, dict[str, Any]] = {}
    for job in job_queue.active_jobs_for_file("llm_process", file_id):
        payload = job.get("payload") or {}
        target = payload.get("target_language", "")
        for step in payload.get("steps") or ["cleanup"]:
            taken.setdefault(step_key(step, target), job)
    return taken


def pending_steps(
    file_id: int, steps: list[str], target_language: str = ""
) -> tuple[list[str], dict[str, Any] | None]:
    """The requested steps nobody is working on yet, plus the job that covers
    the first requested step when there is one.

    Enqueueing a step twice only overwrites the first result and blocks the LLM
    lane for nothing — but a step that is merely *next to* a running one has to
    start, otherwise a translation requested during a cleanup is lost silently.
    """
    taken = active_steps(file_id)
    remaining = [s for s in steps if step_key(s, target_language) not in taken]
    covering = taken.get(step_key(steps[0], target_language)) if steps else None
    return remaining, covering


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
    steps, covering = pending_steps(file_id, steps, target)
    if not steps:  # the user already started it by hand
        return covering
    return job_queue.enqueue(
        "llm_process",
        payload={"file_id": file_id, "steps": steps, "target_language": target, "model": ""},
        file_id=file_id,
        project_id=file_row["project_id"],
        session_id=session_id,
    )


# ── pipeline steps ────────────────────────────────────────────────────


NO_TEXT_MESSAGE = "Das Transkript enthält keinen Text — bitte neu transkribieren"


def segments_text(segments: list[dict[str, Any]]) -> str:
    """The transcript as plain text, blank segments left out.

    A recording without speech yields segments that carry no text. Joined
    unfiltered they look like content ("\\n\\n\\n"), and every step downstream
    then works on nothing: the LLM answers that it was given no text, and the
    PDF export puts a header over an empty page.
    """
    return "\n".join(s["text"].strip() for s in segments if s["text"].strip())


def _refuse_empty(result: str, step: str) -> None:
    """Never store an empty result.

    A stored empty text counts as "done" everywhere afterwards: the PDF export
    builds on it and comes out blank, and the file looks processed. Failing the
    job instead puts the reason in front of the user.
    """
    if not result.strip():
        raise RuntimeError(f"{step} ohne Ergebnis — das LLM hat keinen Text geliefert")


# ── the promise of a verbatim type ───────────────────────────────────
#
# A song, a poem, a speech, a roleplay: the cleanup corrects spelling and
# drops filler words, and that is all it may do. Whether it did is not left to
# the prompt alone — a model that summarizes a section instead of cleaning it
# gives itself away by how little of the section is left in its answer.
# Measured on the distinct words of four letters or more (short filler words
# are exactly what may disappear), a real cleanup keeps nearly all of them,
# where a summary keeps a fraction. Half is a wide margin either way.
MIN_KEPT_SHARE = 0.5

SUMMARIZED_MESSAGE = (
    "Das Modell hat Abschnitt {index}/{total} zusammengefasst statt ihn zu bereinigen — "
    "bei diesem Transkripttyp muss der Text vollständig zurückkommen. Bitte ein anderes "
    "Modell verwenden oder „Text unverändert übernehmen“ prüfen"
)


def _keeps_the_text(source: str, answer: str) -> bool:
    """Whether an answer still carries the section it was given."""
    source_words = llm.word_set(source)
    if len(source_words) < llm.MIN_WORDS_TO_JUDGE:
        return True
    return len(source_words & llm.word_set(answer)) >= MIN_KEPT_SHARE * len(source_words)


# How much of a step's progress the reading of the whole transcript may take.
# The digests are short answers over the same text the step itself works on,
# so they are the cheaper half by a wide margin.
OVERVIEW_PROGRESS_SHARE = 30


def cleanup_segments(
    segments: list[dict[str, Any]],
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
    *,
    verbatim: bool = True,
    glossary: str = "",
    limit: SizeLimit | None = None,
) -> str:
    """LLM cleanup of raw segments, chunk by chunk — also used by the public API.

    Every sentence comes back: the text is worked on in chunks and nothing
    here condenses. `glossary` is what the whole recording is called and how
    it spells its names (`overview.Overview.as_glossary`), which is all a step
    that has to reproduce its material may be shown; the caller decides
    whether that reading is worth its calls. `verbatim` says whether the type
    promises to reproduce, and only governs the guard below.
    """
    chunks = chunking.chunk_segments(segments)
    bare_prompt = CLEANUP_SYSTEM_PROMPT
    if type_prompt:
        bare_prompt += TYPE_CONTEXT_HEADING + type_prompt
    # only the recording's name and its spellings, never the summary: this
    # step has to hand back every sentence of its section
    system_prompt = bare_prompt + glossary

    parts: list[str] = []
    limit = limit if limit is not None else SizeLimit()
    for i, chunk in enumerate(chunks):
        if cancel.is_set():
            raise JobCancelled()
        report_step(report, progress_range, i, len(chunks), "Bereinigung")
        pieces = chat_pieces(
            system_prompt, chunk.own_text, model_override, chunk.context_text, limit
        )
        if verbatim and not _keeps_the_text(chunk.own_text, "\n".join(pieces)):
            # Asked again without the orientation, which is the likeliest
            # reason a model shortened its section towards the whole — so only
            # where there was one. A type that condenses on purpose is never
            # judged this way.
            if glossary:
                logger.warning(
                    "cleanup answer for chunk %d/%d kept too little of it — asking again "
                    "without the overview",
                    i + 1,
                    len(chunks),
                )
                pieces = chat_pieces(
                    bare_prompt, chunk.own_text, model_override, chunk.context_text, limit
                )
            if not glossary or not _keeps_the_text(chunk.own_text, "\n".join(pieces)):
                raise RuntimeError(SUMMARIZED_MESSAGE.format(index=i + 1, total=len(chunks)))
        parts.extend(pieces)

    return "\n\n".join(parts)


def whole_text_overview(
    file_id: int,
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int],
    limit: SizeLimit | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> overview.Overview:
    """The file's overview, and the title it found for the recording.

    The single place a step asks for the whole recording: read once per file
    and kept, so the steps of one job — and a translation started later —
    share it. The title is offered to the file right here: everything else
    about the recording is stated once, and so is its name.

    The transcript is only fetched when there is nothing to reuse — a caller
    that has it already hands it over, one that does not (a translation from
    a stored cleanup) never reads it for a cache hit.
    """
    whole = overview.ensure(
        file_id,
        lambda: transcripts.list_segments(file_id) if segments is None else segments,
        type_prompt,
        model_override,
        cancel,
        report,
        progress_range,
        limit,
    )
    if whole.title:
        workspace.apply_suggested_title(file_id, whole.title)
    return whole


def run_cleanup(
    file_id: int,
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
    verbatim: bool = True,
    whole: overview.Overview | None = None,
) -> tuple[str, overview.Overview]:
    """The cleanup step for one file, and the reading it was done with.

    Which way it runs is decided here, where the transcript type is known: a
    type that reproduces its material is cleaned chunk by chunk, a type that
    writes something of its own has its instruction run once over the whole
    recording.
    """
    segments = transcripts.list_segments(file_id)
    if not segments:
        raise RuntimeError("No segments — transcribe the file first")
    if not segments_text(segments):
        raise RuntimeError(NO_TEXT_MESSAGE)

    limit = SizeLimit()  # what this model can answer is learned once per step
    reading, working = split_range(progress_range, OVERVIEW_PROGRESS_SHARE)
    if whole is None:
        whole = whole_text_overview(
            file_id, type_prompt, model_override, cancel, report, reading, limit, segments
        )
    if not verbatim and whole.digests:
        result = overview.reduce_document(
            whole.digests, type_prompt, model_override, cancel, report, working, limit
        )
    else:
        result = cleanup_segments(
            segments,
            type_prompt,
            model_override,
            cancel,
            report,
            working,
            verbatim=verbatim,
            glossary=whole.as_glossary(),
            limit=limit,
        )
    _refuse_empty(result, "Bereinigung")
    save_text(file_id, "cleanup", result, model=model_override)
    return result, whole


def run_translation(
    file_id: int,
    source_text: str,
    target_language: str,
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
    glossary: str = "",
) -> str:
    system_prompt = TRANSLATE_SYSTEM_PROMPT.format(language=language_name(target_language))
    # what the type says about the transcript decides the wording just as much
    # as it does for the cleanup — that is where "this is a sermon" is written
    if type_prompt:
        system_prompt += TYPE_CONTEXT_HEADING + type_prompt
    # the terms of the whole recording work as a glossary here: without them
    # the same name arrives transliterated three different ways across chunks.
    # The summary stays out — a translation has to be complete, too.
    system_prompt += glossary

    chunks = _chunk_text(source_text)
    parts: list[str] = []
    limit = SizeLimit()
    for i, chunk in enumerate(chunks):
        if cancel.is_set():
            raise JobCancelled()
        report_step(report, progress_range, i, len(chunks), "Übersetzung")
        parts.extend(chat_pieces(system_prompt, chunk, model_override, limit=limit))

    result = "\n\n".join(parts)
    _refuse_empty(result, "Übersetzung")
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
    # the file's own type where it has one, its project's otherwise — a
    # project may hold a song next to an interview
    type_row = project_types.for_file(project, file_row)
    type_prompt = type_row.get("type_prompt") or ""
    # whether this type reproduces its material or writes something of its
    # own decides how the cleanup runs; a file without a type reproduces
    verbatim = project_types.is_verbatim(type_row)

    total_steps = len(steps)
    cleaned: str | None = None
    # the reading of the recording is shared by the steps of this job, so a
    # translation behind a cleanup neither reads nor condenses it again
    whole: overview.Overview | None = None

    for step_index, step in enumerate(steps):
        lo = 100 * step_index // total_steps
        hi = 100 * (step_index + 1) // total_steps
        if step == "cleanup":
            cleaned, whole = run_cleanup(
                file_id, type_prompt, model_override, cancel, report, (lo, hi), verbatim, whole
            )
        elif step == "translate":
            target = payload.get("target_language") or DEFAULT_TARGET_LANGUAGE
            source = cleaned
            if source is None:
                existing = get_text(file_id, "cleanup")
                source = existing["content"] if existing else None
            if source is None:
                source = segments_text(transcripts.list_segments(file_id))
            if not source.strip():
                raise RuntimeError(NO_TEXT_MESSAGE)
            reading, working = split_range((lo, hi), OVERVIEW_PROGRESS_SHARE)
            if whole is None:
                whole = whole_text_overview(
                    file_id, type_prompt, model_override, cancel, report, reading
                )
            run_translation(
                file_id,
                source,
                target,
                type_prompt,
                model_override,
                cancel,
                report,
                working,
                whole.as_glossary(),
            )
        else:
            raise RuntimeError(f"Unknown pipeline step: {step}")

    report(100, "Aufbereitung abgeschlossen")
