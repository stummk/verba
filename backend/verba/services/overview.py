"""The whole recording in one bounded text: map, fold, reduce.

Every LLM step of the pipeline works on chunks, because a two-hour recording
does not fit into any context window. For a step that is *local* — cleaning
up, translating — that is right: each chunk carries its own sentences and
nothing else. For everything the document has only once — a title, a summary,
the list of decisions — it is wrong: run per chunk, the same instruction
produces four titles for a file that was split into four chunks.

So the whole transcript is condensed first, and every call stays inside the
context window while doing it:

1. map    — every chunk becomes one digest of at most `DIGEST_MAX_CHARS`
            (topic, points, names, decisions, tasks, quotations).
2. fold    — while the digests together are larger than one call may be
            given, neighbouring digests are merged into one digest of that
            same size. Each level therefore shrinks by roughly
            `budget / DIGEST_MAX_CHARS`, which is what makes this terminate.
3. reduce — one single call over what is left: either the overview
            (`title`, `summary`, `topics`, `terms`) that the chunk-local
            steps get as orientation, or — for a transcript type that turns
            its material into something else — the finished document itself.

The result is cached per file (`file_overviews`), because cleanup,
translation and export each want it and a recording is condensed once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import db
from ..core.jobs import JobCancelled, report_step, split_range
from . import chunking, llm
from .media import format_clock

if TYPE_CHECKING:  # pipeline imports this module, so only for the annotations
    from .pipeline import SizeLimit

logger = logging.getLogger(__name__)

# One digest may fill a sixth of what a call is given, so at least half a
# dozen sections reach a fold level before it has to fold again.
DIGEST_MAX_CHARS = 1200

# What one call may be given. The same size the chunker uses, because it is
# the same question: how much does a small local model manage at once.
FOLD_BUDGET = chunking.DEFAULT_MAX_CHARS

# The title is offered to the file's header line, which the PDF prints as one
# line with the date flush right — about ninety characters fit there.
TITLE_MAX_CHARS = 80

DIGEST_SYSTEM_PROMPT = (
    "You condense one section of a longer transcription so that a later step can write "
    "one document about the whole recording. Keep, and only where the section holds it: "
    "its topic in one line; the essential points as short bullets; names, terms and "
    "spellings exactly as they appear; decisions; tasks with who and by when; and "
    "sentences that have to be quoted verbatim. Leave out filler and repetition, invent "
    "nothing, and do not write *about* the section — write what it says. Reply in the "
    "language of the transcription, in at most {max_chars} characters, without commentary."
)

FOLD_SYSTEM_PROMPT = (
    "You merge the digests of consecutive sections of one recording into a single digest "
    "of the same kind. Keep their order, and keep every decision, every task, every name "
    "and every verbatim quotation — condense only the prose around them. Reply in the "
    "language of the digests, in at most {max_chars} characters, without commentary."
)

OVERVIEW_SYSTEM_PROMPT = (
    "You state what one recording is about, from the digests of all of its sections. "
    "Reply only with a JSON object, without commentary or Markdown fences:\n"
    '{"title": "...", "summary": "...", "topics": ["..."], "terms": ["..."]}\n'
    '"title": one heading for the whole recording, at most 80 characters, in the language '
    'of the transcription — no quotation marks, no file name, no "Transcript of". '
    '"summary": three to five sentences about the whole recording. "topics": what its '
    'sections are about, in order, one short line each. "terms": names, proper nouns and '
    "technical terms in the spelling the recording uses. Use only what the digests say."
)

# The instruction of a transcript type that does not reproduce its material
# (`project_types.verbatim` off) runs here — once, over everything — instead
# of once per chunk.
DOCUMENT_SYSTEM_PROMPT = (
    "You write one document from a complete transcription. The material reaches you as "
    "the digests of the recording's consecutive sections; together they are the whole "
    "recording. So the result is one single document for all of it: one title, one "
    "continuous line of thought, and every list — decisions, to-dos — exactly once, "
    "merged over all sections. Never repeat a heading or a list per section, and never "
    "mention the sections or the digests themselves. Use only what the material says, "
    "keep names, terms and quotations in the form it gives them, and reply in the "
    "language of the transcription without commentary."
)
DOCUMENT_INSTRUCTION_HEADING = "\n\nWhat the document has to be:\n"

TRUNCATED_DOCUMENT_MESSAGE = (
    "Das Modell konnte das Dokument nicht in einem Stück liefern — bitte ein Modell mit "
    "größerem Kontextfenster verwenden"
)


# ── the overview of one recording ─────────────────────────────────────


GLOSSARY_RULE = (
    "The following is background about the recording your section belongs to. Use it "
    "only to spell names and terms the way the recording does. Never output it, never "
    "shorten your section towards it, and never add a heading from it that your section "
    "does not carry."
)

CONTEXT_RULE = (
    "The following describes the whole recording, of which you are given one section. It "
    "is background: keep names, terms and spellings consistent with it, put your section "
    "where it belongs in the whole, but never output it and never summarize it."
)


@dataclass
class Overview:
    """What a recording is about, plus the material it was read from.

    `digests` is that material: entries in recording order, each of which
    fits into one call. Normally one condensed digest per chunk — and, for a
    recording that fit into a single call to begin with, its text itself.
    """

    title: str = ""
    summary: str = ""
    topics: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    digests: list[str] = field(default_factory=list)

    def as_glossary(self) -> str:
        """What a step that has to hand back every sentence may be told.

        Only the recording's name and the spellings it uses, because that is
        the whole point here: the same person must not be spelled three ways
        across chunk boundaries. Deliberately without the summary — nothing in
        the input of a cleanup or a translation should read like a shorter
        version of the text it is working on, and a song, a poem or a speech
        has to come back word for word.
        """
        lines = []
        if self.title:
            lines.append(f"Title of the whole recording: {self.title}")
        if self.terms:
            lines.append("Names and spellings it uses: " + ", ".join(self.terms))
        return _background(GLOSSARY_RULE, lines)

    def as_context(self) -> str:
        """The whole picture — for a step that writes something of its own.

        Only reached where the text is being restructured anyway (the export's
        structure stage of a non-verbatim type), never where it has to be
        reproduced.
        """
        lines = []
        if self.title:
            lines.append(f"Title: {self.title}")
        if self.summary:
            lines.append(f"About: {self.summary}")
        if self.topics:
            lines.append("Sections: " + "; ".join(self.topics))
        if self.terms:
            lines.append("Names and spellings: " + ", ".join(self.terms))
        return _background(CONTEXT_RULE, lines)


def _background(rule: str, lines: list[str]) -> str:
    """A block appended to a system prompt — empty when it says nothing."""
    if not lines:
        return ""
    return "\n\n" + rule + "\n" + "\n".join(lines)


# ── the pieces of the machinery ──────────────────────────────────────


def _cap(text: str, max_chars: int) -> str:
    """Cut over-long text back on a boundary.

    A model that ignores its character budget must not push a fold level over
    the size a call may have — the guarantee that nothing overruns the context
    window is ours, not the model's.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    for boundary in ("\n", ". ", " "):
        cut = head.rfind(boundary)
        if cut > max_chars // 2:
            return head[:cut].rstrip()
    return head.rstrip()


def _label(index: int, total: int, chunk: chunking.Chunk) -> str:
    """Where a digest sits in the recording, for the reduce step to order by."""
    label = f"Section {index + 1}/{total}"
    start, end = chunk.time_range
    if not end:  # segments without timestamps (a transcription in progress)
        return label
    return f"{label} ({format_clock(start)}–{format_clock(end)})"


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [" ".join(str(entry).split()) for entry in value]
    return [item for item in items if item][:limit]


def section_digests(
    chunks: list[chunking.Chunk],
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int],
    limit: SizeLimit | None = None,
) -> list[str]:
    """One digest per chunk — the map step."""
    from . import pipeline  # cycle: pipeline is what runs the steps

    limit = limit if limit is not None else pipeline.SizeLimit()
    system_prompt = DIGEST_SYSTEM_PROMPT.format(max_chars=DIGEST_MAX_CHARS)
    if type_prompt:
        # what the type will make of the material decides what a digest has to
        # carry — minutes need the decisions, a sermon the wording
        system_prompt += pipeline.TYPE_CONTEXT_HEADING + type_prompt

    digests: list[str] = []
    for index, chunk in enumerate(chunks):
        if cancel.is_set():
            raise JobCancelled()
        report_step(report, progress_range, index, len(chunks), "Überblick")
        # No overlap context here, unlike the cleanup: a digest does not have
        # to join a sentence back together, and the previous section's text in
        # front of this one would only crowd out its own content.
        pieces = pipeline.chat_pieces(system_prompt, chunk.own_text, model_override, "", limit)
        text = _cap("\n".join(piece for piece in pieces if piece.strip()), DIGEST_MAX_CHARS)
        if text:
            digests.append(f"{_label(index, len(chunks), chunk)}\n{text}")
    return digests


def _condense(digests: list[str], model_override: str, max_chars: int = DIGEST_MAX_CHARS) -> str:
    """One fold call: several digests become one of at most `max_chars`."""
    system_prompt = FOLD_SYSTEM_PROMPT.format(max_chars=max_chars)
    try:
        answer = llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n\n".join(digests)},
            ],
            model_override=model_override,
        )
    except llm.TruncatedAnswer as exc:
        # A cut-off digest is still a digest — unlike a cut-off transcript,
        # which `chat_pieces` has to split for. What came back is used, and
        # the cap below keeps the level shrinking either way.
        logger.warning("fold answer was cut off — using the part that arrived")
        answer = exc.text
    return _cap(answer, max_chars)


def _batches(digests: list[str], budget: int) -> list[list[str]]:
    """Group neighbouring digests into portions one call may be given.

    A portion is filled to the budget but always holds at least two digests,
    so every level has fewer entries than the one before it and the fold
    cannot spin. A single digest left over at the end stays on its own — put
    into the last portion it would push that one over the budget, which is
    the one thing this must never do.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for digest in digests:
        if len(current) >= 2 and size + len(digest) > budget:
            batches.append(current)
            current, size = [], 0
        current.append(digest)
        size += len(digest) + 2
    if current:
        batches.append(current)
    return batches


def fold(
    digests: list[str],
    model_override: str,
    cancel: threading.Event,
    budget: int = FOLD_BUDGET,
) -> str:
    """Condense the digests until they fit into one call — the fold step.

    Terminates because `_batches` groups at least two entries into every
    portion but the last, so each level is strictly shorter than the one
    before it and this ends at a single entry at the latest.
    """
    level = [digest for digest in digests if digest.strip()]
    if len(level) > 1:
        # Where several digests have to be merged, none of them may be larger
        # than half of what a call is given, so that two always fit into one —
        # otherwise a model that manages only small pieces (`SizeLimit`) would
        # leave portions the fold cannot shrink. A single digest is the whole
        # material and is passed on as it is.
        level = [_cap(digest, budget // 2) for digest in level]
    while len(level) > 1 and len("\n\n".join(level)) > budget:
        next_level: list[str] = []
        for batch in _batches(level, budget):
            if cancel.is_set():
                raise JobCancelled()
            next_level.append(_condense(batch, model_override) if len(batch) > 1 else batch[0])
        level = [entry for entry in next_level if entry.strip()]
    return _cap("\n\n".join(level), budget)


# ── building it ──────────────────────────────────────────────────────


def build(
    segments: list[dict[str, Any]],
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
    limit: SizeLimit | None = None,
) -> Overview:
    """Read the whole transcript and say what it is about.

    A transcript that fits into one call is its own digest — there is nothing
    to condense, and the overview is one call over the text itself.
    """
    from . import pipeline

    limit = limit if limit is not None else pipeline.SizeLimit()
    chunks = chunking.chunk_segments(segments)
    if not chunks:
        return Overview()

    if len(chunks) == 1:
        digests = [_cap(chunks[0].own_text, FOLD_BUDGET)]
    else:
        mapping, _rest = split_range(progress_range, 80)  # the map is the long part
        digests = section_digests(
            chunks, type_prompt, model_override, cancel, report, mapping, limit
        )
    if not digests:
        return Overview()

    overview = Overview(digests=digests)
    _describe(overview, model_override, cancel, report, progress_range)
    return overview


def _describe(
    overview: Overview,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int],
) -> bool:
    """Fill in title, summary, topics and terms from the material.

    The one call the whole reading exists for, kept separate so it can be
    made again for material that is already condensed: an endpoint that
    hiccups here must not cost a recording its title for good.
    """
    if cancel.is_set():
        raise JobCancelled()
    lo, hi = progress_range
    report(max(lo, hi - 1), "Überblick zusammenfassen")
    try:
        answer = llm.chat(
            [
                {"role": "system", "content": OVERVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": fold(overview.digests, model_override, cancel)},
            ],
            model_override=model_override,
        )
    except llm.LLMError as exc:
        # The overview is orientation, not content: a step that has the
        # digests can work without a title. Only the digests must be right.
        logger.warning("overview answer unusable (%s) — continuing without it", exc)
        return False

    data = llm.json_object(answer) or {}
    # The title goes into the file's header line, which the PDF prints as one
    # line with the date flush right — so it is cut to what that line holds,
    # whatever the prompt asked for.
    overview.title = " ".join(str(data.get("title", "")).split())[:TITLE_MAX_CHARS]
    overview.summary = _cap(str(data.get("summary", "")), 600)
    overview.topics = _string_list(data.get("topics"), 20)
    overview.terms = _string_list(data.get("terms"), 40)
    if not (overview.title or overview.summary or overview.topics):
        logger.warning("overview answer carried no fields — continuing without it")
        return False
    return True


def reduce_document(
    digests: list[str],
    instruction: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
    limit: SizeLimit | None = None,
) -> str:
    """One document over the whole recording — the reduce step.

    Deliberately not `pipeline.chat_pieces`: splitting the input is what
    produces several documents. A cut-off answer is answered with a smaller
    input instead — the material already folded is condensed one step
    further, which costs one call rather than another fold from scratch —
    and after that with a clear failure.
    """
    from . import pipeline

    system_prompt = DOCUMENT_SYSTEM_PROMPT
    if instruction.strip():
        system_prompt += DOCUMENT_INSTRUCTION_HEADING + instruction.strip()

    limit = limit if limit is not None else pipeline.SizeLimit()
    material = fold(digests, model_override, cancel, budget=limit.max_chars or FOLD_BUDGET)
    lo, hi = progress_range
    for attempt in range(2):
        if cancel.is_set():
            raise JobCancelled()
        if attempt:
            material = _condense([material], model_override, max_chars=len(material) // 2)
        report(lo + (hi - lo) * attempt // 2, "Dokument erstellen")
        try:
            return llm.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": material},
                ],
                model_override=model_override,
            ).strip()
        except llm.TruncatedAnswer:
            logger.warning("document answer was cut off — condensing further and asking again")
            limit.shrink_to(len(material) // 2)
    raise RuntimeError(TRUNCATED_DOCUMENT_MESSAGE)


# ── cache ────────────────────────────────────────────────────────────


def context_key(type_prompt: str) -> str:
    """What the digests were condensed under. A changed transcript type asks
    different things of a digest, so the cached one no longer answers."""
    return hashlib.sha256(type_prompt.encode("utf-8")).hexdigest()[:16]


def load(file_id: int, type_prompt: str | None = None) -> Overview | None:
    """The stored overview of a file, or None — also None when it was
    condensed under a different transcript type than the one asked for."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM file_overviews WHERE file_id = ?", (file_id,)).fetchone()
    if row is None:
        return None
    if type_prompt is not None and row["context_key"] != context_key(type_prompt):
        return None
    return Overview(
        title=row["title"],
        summary=row["summary"],
        topics=_string_list(json.loads(row["topics"] or "[]"), 20),
        terms=_string_list(json.loads(row["terms"] or "[]"), 40),
        digests=[str(entry) for entry in json.loads(row["digests"] or "[]")],
    )


def save(file_id: int, overview: Overview, type_prompt: str) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO file_overviews "
            "(file_id, title, summary, topics, terms, digests, context_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_id) DO UPDATE SET title = excluded.title, "
            "summary = excluded.summary, topics = excluded.topics, terms = excluded.terms, "
            "digests = excluded.digests, context_key = excluded.context_key, "
            "created_at = datetime('now')",
            (
                file_id,
                overview.title,
                overview.summary,
                json.dumps(overview.topics, ensure_ascii=False),
                json.dumps(overview.terms, ensure_ascii=False),
                json.dumps(overview.digests, ensure_ascii=False),
                context_key(type_prompt),
            ),
        )


def invalidate(file_id: int) -> None:
    """Drop the overview — the transcript it was read from has changed."""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM file_overviews WHERE file_id = ?", (file_id,))


def ensure(
    file_id: int,
    segments: Callable[[], list[dict[str, Any]]],
    type_prompt: str,
    model_override: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
    limit: SizeLimit | None = None,
) -> Overview:
    """The file's overview, read once and kept.

    Cleanup, translation and export all want it; a recording is condensed
    once and stays condensed until its segments change. `segments` is asked
    for the transcript only when there is nothing to reuse — on the common
    path this costs one SELECT, not one per step.

    A cached reading whose describing call had failed is described again: the
    digests are the expensive part and they are kept, but a recording must not
    lose its title for good to one bad answer.
    """
    cached = load(file_id, type_prompt)
    if cached is not None and cached.digests:
        if cached.title or cached.summary or cached.topics:
            return cached
        if _describe(cached, model_override, cancel, report, progress_range):
            save(file_id, cached, type_prompt)
        return cached
    overview = build(segments(), type_prompt, model_override, cancel, report, progress_range, limit)
    if overview.digests:
        save(file_id, overview, type_prompt)
    return overview
