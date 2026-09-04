"""PDF export: two-stage template pipeline.

Stage 1 (structure): turn the best available text of a file into a neutral
block structure — via the configured LLM (strict JSON output) or, without
one, rule-based from paragraphs/segments. Export therefore always works;
with an LLM it gets smarter (stanzas, dialogue roles, minutes with to-dos).

Stage 2 (render): a deterministic fpdf2 renderer lays the blocks out
according to the transcript type's template. Folder exports append each
file as a section separated by spacing only — no table of contents, no
extra section titles; the per-file header comes from file metadata. A type
may ask for its sections to be kept whole (`keep_sections`): a file that
would not fit on the rest of the page then starts on a new one, together
with its translations.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.jobs import JobCancelled, job_queue
from . import llm, pipeline, transcripts, workspace
from .metadata import format_display_date

logger = logging.getLogger(__name__)

# How a transcript type is laid out. Chosen per type (project_types.structure)
# instead of being wired to fixed type keys, so custom types can pick one too:
#
# paragraphs  running text — the default
# stanzas     line-preserving verses (song, poem)
# dialogue    spoken by several roles: built from the segments (which carry the
#             speakers) instead of the flattened cleanup text
# script      dialogue, plus character names set in capitals (play, roleplay)
STRUCTURES = ("paragraphs", "stanzas", "dialogue", "script")
DEFAULT_STRUCTURE = "paragraphs"
DIALOGUE_STRUCTURES = {"dialogue", "script"}


def normalize_structure(value: str | None) -> str:
    """Fall back to the default for anything unknown (or a project without a
    type), so the export never depends on a valid value being stored."""
    return value if value in STRUCTURES else DEFAULT_STRUCTURE


BLOCK_KINDS = {"heading", "paragraph", "stanza", "dialogue", "list", "separator"}

# The block contract every output prompt has to honour — `_parse_blocks()`
# below is what actually enforces it. Transcript types may replace the prose
# around it (`project_types.output_prompt`); an answer that cannot be parsed
# always falls back to rule-based structuring, so a broken prompt degrades the
# export instead of breaking it.
BLOCK_CONTRACT = (
    "Reply only with a JSON array of blocks without commentary or Markdown fences. "
    "Allowed blocks:\n"
    '{"kind": "heading", "text": "..."} - subheading\n'
    '{"kind": "paragraph", "text": "..."} - paragraph\n'
    '{"kind": "stanza", "lines": ["..."]} - stanza (poem/song), one line at a time\n'
    '{"kind": "dialogue", "speaker": "...", "text": "..."} - spoken contribution\n'
    '{"kind": "list", "title": "...", "items": ["..."]} - list (e.g. decisions, to-dos)\n'
    '{"kind": "separator"} - separator between sections'
)

# Applies whenever a transcript type defines no output prompt of its own. New
# types are pre-filled with it, so it can be adapted instead of guessed at.
DEFAULT_OUTPUT_PROMPT = (
    "You structure a transcript for PDF export. "
    + BLOCK_CONTRACT
    + "\nReproduce the full text; do not summarize or omit anything unless the "
    "transcript type context explicitly requires it."
)


# ── stage 1: structure ────────────────────────────────────────────────


def _base_text(file_id: int, structure: str, language: str) -> str:
    """The text a structure is built from, best variant first.

    A variant only counts when it actually carries content — an empty derived
    text (from a failed LLM run) or a transcript whose segments are all blank
    would otherwise reach the structure stage as nothing but whitespace: the
    LLM answers that it was given no text, and the rule-based fallback yields
    no blocks at all. Both end as a PDF with a header and nothing under it.
    """
    if language:
        text = pipeline.get_text(file_id, "translation", language)
        if text is None or not text["content"].strip():
            raise RuntimeError(f"Keine Übersetzung ({language}) vorhanden")
        return text["content"]
    if structure in DIALOGUE_STRUCTURES:
        segments = transcripts.list_segments(file_id)
        lines = []
        for segment in segments:
            speaker = (segment.get("speaker") or "").strip()
            text = segment["text"].strip()
            if not text:  # a blank segment carries no contribution
                continue
            lines.append(f"{speaker}: {text}" if speaker else text)
        if lines:
            return "\n".join(lines)
    cleanup = pipeline.get_text(file_id, "cleanup")
    if cleanup is not None and cleanup["content"].strip():
        return cleanup["content"]
    segments = transcripts.list_segments(file_id)
    if not segments:
        raise RuntimeError("Keine Segmente — die Datei muss zuerst transkribiert werden")
    text = pipeline.segments_text(segments)
    if not text:
        raise RuntimeError(pipeline.NO_TEXT_MESSAGE)
    return text


def _json_arrays(raw: str) -> list[list[Any]]:
    """Every JSON array in an LLM answer, in order.

    A local model rarely answers with exactly one array: it wraps it in prose
    or a Markdown fence, or emits one array per paragraph. Decoding from each
    `[` and skipping what does not parse takes all of those, where taking the
    span from the first `[` to the last `]` took none of them.
    """
    decoder = json.JSONDecoder()
    arrays: list[list[Any]] = []
    index = 0
    while (start := raw.find("[", index)) >= 0:
        try:
            data, end = decoder.raw_decode(raw, start)
        except ValueError:
            index = start + 1
            continue
        index = end
        if isinstance(data, list):
            arrays.append(data)
    return arrays


def _parse_blocks(raw: str) -> list[dict[str, Any]] | None:
    """Extract and validate the JSON blocks from an LLM answer."""
    raw_items = [item for array in _json_arrays(raw) for item in array]
    if not raw_items:
        return None
    blocks: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict) or item.get("kind") not in BLOCK_KINDS:
            continue
        kind = item["kind"]
        if kind == "stanza":
            lines = [str(line) for line in item.get("lines", []) if str(line).strip()]
            if lines:
                blocks.append({"kind": "stanza", "lines": lines})
        elif kind == "dialogue":
            text = str(item.get("text", "")).strip()
            if text:
                blocks.append(
                    {
                        "kind": "dialogue",
                        "speaker": str(item.get("speaker", "")).strip(),
                        "text": text,
                    }
                )
        elif kind == "list":
            items = [str(entry) for entry in item.get("items", []) if str(entry).strip()]
            if items:
                blocks.append(
                    {"kind": "list", "title": str(item.get("title", "")).strip(), "items": items}
                )
        elif kind == "separator":
            blocks.append({"kind": "separator"})
        else:  # heading, paragraph
            text = str(item.get("text", "")).strip()
            if text:
                blocks.append({"kind": kind, "text": text})
    return blocks or None


def blocks_text(blocks: list[dict[str, Any]]) -> str:
    """All text a block list carries — for checking, not for rendering."""
    parts: list[str] = []
    for block in blocks:
        parts.append(str(block.get("title", "")))
        parts.append(str(block.get("speaker", "")))
        parts.append(str(block.get("text", "")))
        parts.extend(str(line) for line in block.get("lines", []))
        parts.extend(str(item) for item in block.get("items", []))
    return " ".join(part for part in parts if part)


_WORD_RE = re.compile(r"\w{4,}")

# A model that ignores its input answers *about* the task instead of doing it
# ("No speech text was provided — please paste the full text of the speech ...").
# Such an answer parses as valid blocks and would be printed as if it were the
# document. It gives itself away through its vocabulary: hardly a word of it
# comes from the transcript. Asking it that way round — how much of the answer
# is covered by the material, not how much of the material survived — leaves a
# type that legitimately condenses (meeting minutes) untouched: a summary is
# written from the transcript's own words.
MIN_SOURCED_SHARE = 0.5
MIN_WORDS_TO_JUDGE = 20  # too little material to tell a refusal from a layout


def _keeps_the_material(blocks: list[dict[str, Any]], source: str) -> bool:
    """Whether structured blocks were built from `source` at all."""
    source_words = set(_WORD_RE.findall(source.lower()))
    if len(source_words) < MIN_WORDS_TO_JUDGE:
        return True
    answer_words = set(_WORD_RE.findall(blocks_text(blocks).lower()))
    if not answer_words:
        return False
    sourced = answer_words & source_words
    return len(sourced) >= MIN_SOURCED_SHARE * len(answer_words)


def _structure_rule_based(text: str, structure: str) -> list[dict[str, Any]]:
    """Deterministic fallback: paragraphs, stanzas or speaker turns from plain text."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if structure == "stanzas":
        return [
            {"kind": "stanza", "lines": [line.strip() for line in p.splitlines() if line.strip()]}
            for p in paragraphs
        ]
    if structure in DIALOGUE_STRUCTURES:
        blocks: list[dict[str, Any]] = []
        for paragraph in paragraphs:
            for line in paragraph.splitlines():
                speaker, sep, spoken = line.partition(":")
                if sep and 0 < len(speaker.strip()) <= 40 and spoken.strip():
                    blocks.append(
                        {"kind": "dialogue", "speaker": speaker.strip(), "text": spoken.strip()}
                    )
                elif line.strip():
                    blocks.append({"kind": "paragraph", "text": line.strip()})
        return blocks
    return [{"kind": "paragraph", "text": p} for p in paragraphs]


def output_system_prompt(output_prompt: str, type_prompt: str) -> str:
    """System prompt for the structure stage.

    The transcript type's output prompt replaces the format instruction; an
    empty one falls back to the default. Its cleanup prompt is appended as
    context either way — it describes what the material is.
    """
    system_prompt = output_prompt.strip() or DEFAULT_OUTPUT_PROMPT
    if type_prompt:
        system_prompt += "\n\nContext of the transcription type:\n" + type_prompt
    return system_prompt


def _structure_llm(
    text: str,
    output_prompt: str,
    type_prompt: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int],
) -> list[dict[str, Any]] | None:
    system_prompt = output_system_prompt(output_prompt, type_prompt)
    chunks = pipeline._chunk_text(text)
    blocks: list[dict[str, Any]] = []
    limit = pipeline.SizeLimit()
    lo, hi = progress_range
    for i, chunk in enumerate(chunks):
        if cancel.is_set():
            raise JobCancelled()
        report(lo + (hi - lo) * i // max(1, len(chunks)), f"Structuring {i + 1}/{len(chunks)}")
        # every answer is parsed on its own: a chunk that had to be split
        # comes back as several JSON arrays, never as one
        chunk_blocks: list[dict[str, Any]] = []
        for answer in pipeline.chat_pieces(system_prompt, chunk, limit=limit):
            parsed = _parse_blocks(answer)
            if parsed is None:
                logger.warning("structuring response not parseable — rule-based fallback")
                return None
            chunk_blocks.extend(parsed)
        if not _keeps_the_material(chunk_blocks, chunk):
            logger.warning(
                "structuring response is not built from the transcript (%d chars in, "
                "%d out) — rule-based fallback",
                len(chunk),
                len(blocks_text(chunk_blocks)),
            )
            return None
        blocks.extend(chunk_blocks)
    return blocks or None


def build_document(
    file_row: dict[str, Any],
    project: dict[str, Any],
    language: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
) -> dict[str, Any]:
    """Stage 1 for one file: title/date from metadata plus structured blocks."""
    # only whether a type is assigned matters here — everything type-specific
    # comes from its own fields (structure, output prompt), never from its key
    has_type = bool(project.get("type_key"))
    structure = normalize_structure(project.get("type_structure"))
    text = _base_text(file_row["id"], structure, language)

    blocks: list[dict[str, Any]] | None = None
    if has_type and llm.llm_location() != "none":
        blocks = _structure_llm(
            text,
            project.get("type_output_prompt") or "",
            project.get("type_prompt") or "",
            cancel,
            report,
            progress_range,
        )
    if blocks is None:
        blocks = _structure_rule_based(text, structure)
    if not blocks:
        # nothing rendered under the header: fail the job instead of handing
        # out a PDF that looks like the transcript came out empty
        name = file_row.get("filename") or Path(file_row["rel_path"]).name
        raise RuntimeError(f"Kein Inhalt für {name} — der Text ließ sich nicht strukturieren")

    return {
        "title": file_row.get("title") or Path(file_row["rel_path"]).stem,
        "date": file_row.get("recorded_at") or "",
        "header_left": file_row.get("header_left") or "",
        "header_middle": file_row.get("header_middle") or "",
        "header_right": format_display_date(file_row.get("header_right") or ""),
        "blocks": blocks,
    }


# ── stage 2: deterministic renderer ──────────────────────────────────


def _font_candidates() -> list[tuple[Path, Path]]:
    if platform.system() == "Windows":
        fonts = Path("C:/Windows/Fonts")
        return [
            (fonts / "arial.ttf", fonts / "arialbd.ttf"),
            (fonts / "segoeui.ttf", fonts / "segoeuib.ttf"),
        ]
    return [
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
        (
            Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
        ),
    ]


def _setup_fonts(pdf: Any) -> str:
    """Register a Unicode system font; helvetica (latin-1) as last resort."""
    for regular, bold in _font_candidates():
        if regular.exists() and bold.exists():
            pdf.add_font("doc", "", str(regular))
            pdf.add_font("doc", "B", str(bold))
            return "doc"
    logger.warning("no Unicode font found — export falls back to Helvetica (Latin-1)")
    return "helvetica"


def _sanitize_for(family: str, text: str) -> str:
    if family == "helvetica":
        return text.encode("latin-1", "replace").decode("latin-1")
    return text


# Vertical spacing in mm, in one place so the layout stays tunable.
GAP_AFTER_HEADER = 1.0  # header line → first block
GAP_BETWEEN_SECTIONS = 5.0  # one file → the next in a folder export
GAP_BEFORE_HEADING = 1.5
GAP_AFTER_HEADING = 0.5
GAP_AFTER_PARAGRAPH = 1.5
GAP_AFTER_STANZA = 2.0
GAP_AFTER_DIALOGUE = 1.0
GAP_AFTER_LIST = 1.5
GAP_AFTER_SEPARATOR = 2.0
GAP_AROUND_DIVIDER = 2.0  # the "---" between language versions of one file
#: How close to the top margin still counts as "the page is empty" — a
#: section that fills more than a page must not push a blank one in front.
AT_PAGE_TOP = 0.5

_HARD_BREAK_RE = re.compile(r"\s*\n\s*")
_SPACES_RE = re.compile(r"[^\S\n]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([,.;:!?)])")
_MISSING_SPACE_RE = re.compile(r"([,;:])(?=[^\s\d/])")


def flow_text(value: str) -> str:
    """Running text with sensible spaces and no line breaks of its own.

    Transcripts and LLM answers carry newlines wherever the source happened to
    wrap — inside a sentence more often than at its end. Rendered as they are,
    they become hard breaks in the middle of a justified line, which is what
    makes an exported paragraph look torn apart. The renderer wraps and
    justifies by itself, so a paragraph is handed over as one line: breaks
    become spaces, runs of spaces collapse, and a space that ended up in front
    of its punctuation (or is missing behind it) is put right.
    """
    value = _HARD_BREAK_RE.sub(" ", value.strip())
    value = _SPACES_RE.sub(" ", value)
    value = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", value)
    return _MISSING_SPACE_RE.sub(r"\1 ", value).strip()


class _Renderer:
    """Deterministic layout of structure blocks according to the type template."""

    def __init__(self, pdf: Any, family: str, structure: str) -> None:
        self.pdf = pdf
        self.family = family
        self.structure = structure

    def _text(self, value: str) -> str:
        return _sanitize_for(self.family, value)

    def divider(self) -> None:
        """Separates the language versions of one file in a combined PDF."""
        pdf = self.pdf
        pdf.ln(GAP_AROUND_DIVIDER)
        pdf.set_font(self.family, "", 11)
        pdf.cell(0, 6, self._text("---"), align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(GAP_AROUND_DIVIDER)

    def _write(self, height: float, value: str, align: str = "L") -> None:
        """Full-width wrapped text; cursor continues at the left margin."""
        self.pdf.multi_cell(
            0, height, self._text(value), align=align, new_x="LMARGIN", new_y="NEXT"
        )

    def _flowing(self, height: float, value: str) -> None:
        """Running text: normalised whitespace, justified like a document."""
        self._write(height, flow_text(value), align="J")

    def section(self, doc: dict[str, Any]) -> None:
        """Render one file's optional header and its blocks."""
        left = doc.get("header_left") or ""
        middle = doc.get("header_middle") or ""
        right = doc.get("header_right") or ""
        if left or middle or right:
            self._header_line(left, middle, right)
        for block in doc["blocks"]:
            self._block(block)

    def group(self, docs: list[dict[str, Any]]) -> None:
        """One file: its section, followed by its other language versions.

        The versions are separated by the divider instead of a header of their
        own — title, addition and date belong to the file, not to a language.
        """
        for index, doc in enumerate(docs):
            if index:
                self.divider()
            self.section(doc)

    def place_group(self, docs: list[dict[str, Any]], keep_together: bool) -> None:
        """Render a file behind the one before it — on a new page if it must.

        `keep_together` is the transcript type's choice: the file's section
        then starts on a new page whenever it would not fit on the current one
        as a whole. Whether it fits is answered by laying it out into a dummy
        document first, so the answer covers everything the section carries —
        the header, every block, and the translations that belong to it.
        """
        if keep_together and not self._fits_here(docs):
            self.pdf.add_page()
            self.group(docs)
            return
        self.pdf.ln(GAP_BETWEEN_SECTIONS)
        self.group(docs)

    def _fits_here(self, docs: list[dict[str, Any]]) -> bool:
        pdf = self.pdf
        if pdf.y <= pdf.t_margin + AT_PAGE_TOP:
            # nothing to gain: this page is where the section starts anyway,
            # and a section longer than a page has to run over regardless
            return True
        with pdf.offset_rendering() as dummy:
            pdf.ln(GAP_BETWEEN_SECTIONS)
            self.group(docs)
        return not dummy.page_break_triggered

    def _header_line(self, left: str, middle: str, right: str) -> None:
        """One line: title, the addition right behind it, the date flush right.

        The cells are only as wide as their text, so the addition sits a
        single space behind the title instead of in a column of its own; the
        date cell takes whatever is left of the line.
        """
        pdf = self.pdf
        page_width = pdf.w - pdf.l_margin - pdf.r_margin
        addition = ""
        if middle:
            addition = f" ({middle})" if left else f"({middle})"

        pdf.set_font(self.family, "B", 11)
        title_width = pdf.get_string_width(self._text(left)) if left else 0
        pdf.set_font(self.family, "", 11)
        addition_width = pdf.get_string_width(self._text(addition)) if addition else 0
        rest = max(page_width - title_width - addition_width, 0)

        if left:
            pdf.set_font(self.family, "B", 11)
            pdf.cell(title_width, 7, self._text(left), align="L")
        if addition:
            pdf.set_font(self.family, "", 11)
            pdf.cell(addition_width, 7, self._text(addition), align="L")
        pdf.set_font(self.family, "B", 11)
        pdf.cell(rest, 7, self._text(right), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(GAP_AFTER_HEADER)

    def _block(self, block: dict[str, Any]) -> None:
        pdf = self.pdf
        kind = block["kind"]
        if kind == "heading":
            pdf.ln(GAP_BEFORE_HEADING)
            pdf.set_font(self.family, "B", 12)
            self._write(7, flow_text(block["text"]))
            pdf.ln(GAP_AFTER_HEADING)
        elif kind == "stanza":
            # the only kind whose line breaks are content, not accidental wrap
            pdf.set_font(self.family, "", 11)
            for line in block["lines"]:
                self._write(6, " ".join(line.split()))
            pdf.ln(GAP_AFTER_STANZA)
        elif kind == "dialogue":
            speaker = flow_text(block.get("speaker") or "")
            if self.structure == "script":
                speaker = speaker.upper()
            if speaker:
                pdf.set_font(self.family, "B", 11)
                self._write(6, speaker)
            pdf.set_font(self.family, "", 11)
            self._flowing(6, block["text"])
            pdf.ln(GAP_AFTER_DIALOGUE)
        elif kind == "list":
            if block.get("title"):
                pdf.set_font(self.family, "B", 11)
                self._write(6, flow_text(block["title"]))
            pdf.set_font(self.family, "", 11)
            for item in block["items"]:
                self._write(6, f"•  {flow_text(item)}")
            pdf.ln(GAP_AFTER_LIST)
        elif kind == "separator":
            pdf.set_font(self.family, "", 11)
            pdf.cell(0, 8, self._text("*   *   *"), align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(GAP_AFTER_SEPARATOR)
        else:  # paragraph
            pdf.set_font(self.family, "", 11)
            self._flowing(6, block["text"])
            pdf.ln(GAP_AFTER_PARAGRAPH)


def section_groups(docs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The documents of one file each, in the order they were built.

    A language version carries `divider` and belongs to the file in front of
    it: original and translations are one section of the export, so a page
    break is decided for all of them together, never between them.
    """
    groups: list[list[dict[str, Any]]] = []
    for doc in docs:
        if doc.get("divider") and groups:
            groups[-1].append(doc)
        else:
            groups.append([doc])
    return groups


def render_pdf(
    docs: list[dict[str, Any]], structure: str, target: Path, keep_sections: bool = False
) -> None:
    """Stage 2: render one or many file sections into a single PDF.

    Multiple sections flow continuously, separated by spacing only — no table
    of contents and no extra section titles (the template header per file is
    the only marker).

    `keep_sections` comes from the transcript type: with it, a file's section
    is not torn across a page boundary but starts on a new page instead.
    """
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RuntimeError("PDF export requires the fpdf2 package — complete setup first") from exc

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    family = _setup_fonts(pdf)
    pdf.set_title(_sanitize_for(family, docs[0]["title"]) if docs else "Verba")
    pdf.add_page()
    renderer = _Renderer(pdf, family, normalize_structure(structure))
    for index, group in enumerate(section_groups(docs)):
        if index:
            renderer.place_group(group, keep_sections)
        else:
            renderer.group(group)
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(target))


# ── job handler & helpers ─────────────────────────────────────────────


def exports_dir(project: dict[str, Any]) -> Path:
    return workspace.project_dir(project) / "exports"


def export_name(stem: str, language: str, combined: bool = False) -> str:
    """File name of an export. `all` marks a combined multi-language PDF —
    not a language code, so it cannot collide with one."""
    if combined:
        return f"{stem}.all.pdf"
    return f"{stem}.{language}.pdf" if language else f"{stem}.pdf"


def translation_languages(file_id: int) -> list[str]:
    """Languages a file has a stored translation for, alphabetically."""
    return sorted(
        text["language"]
        for text in pipeline.list_texts(file_id)
        if text["kind"] == "translation" and text["language"]
    )


def list_exports(project: dict[str, Any]) -> list[dict[str, Any]]:
    directory = exports_dir(project)
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*.pdf")):
        stat = path.stat()
        entries.append({"name": path.name, "size": stat.st_size, "modified": int(stat.st_mtime)})
    return entries


def _file_documents(
    file_row: dict[str, Any],
    project: dict[str, Any],
    languages: list[str],
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int],
) -> list[dict[str, Any]]:
    """One document per requested language version of a single file.

    With more than one version the first is the original and the rest follow
    it in the same PDF: they carry a divider instead of the file header, which
    would otherwise repeat identically (title, addition and place/date are
    metadata of the file, not of the language).
    """
    docs: list[dict[str, Any]] = []
    lo, hi = progress_range
    span = max(hi - lo, 1)
    for index, language in enumerate(languages):
        if cancel.is_set():
            raise JobCancelled()
        start = lo + span * index // len(languages)
        end = lo + span * (index + 1) // len(languages)
        doc = build_document(file_row, project, language, cancel, report, (start, end))
        if index:
            doc.update(
                {"header_left": "", "header_middle": "", "header_right": "", "divider": True}
            )
        docs.append(doc)
    return docs


def handle_export_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    """Job handler: export one file or a whole project as PDF.

    Payload: {"scope": "file"|"project", "file_id"?, "project_id"?,
              "language": "", "combine": false}

    `combine` puts the original and every stored translation into one PDF,
    each version separated by a divider line; otherwise one version is
    exported on its own.
    """
    payload = job["payload"]
    scope = payload.get("scope", "file")
    language = payload.get("language", "")
    combine = bool(payload.get("combine"))

    def languages_for(file_id: int) -> list[str]:
        if not combine:
            return [language]
        return ["", *translation_languages(file_id)]

    if scope == "project":
        project = workspace.get_project(int(payload["project_id"]))
        if project is None:
            raise RuntimeError("Transcript no longer exists")
        files = [f for f in workspace.list_files(project["id"]) if f["status"] == "done"]
        if not files:
            raise RuntimeError("No transcribed files available")
        docs = []
        for index, file_row in enumerate(files):
            if cancel.is_set():
                raise JobCancelled()
            lo = 90 * index // len(files)
            hi = 90 * (index + 1) // len(files)
            report(lo, f"Datei {index + 1}/{len(files)}: {file_row['filename']}")
            docs.extend(
                _file_documents(
                    file_row, project, languages_for(file_row["id"]), cancel, report, (lo, hi)
                )
            )
        target = exports_dir(project) / export_name(project["slug"], language, combine)
    else:
        file_row = workspace.get_file(int(payload["file_id"]))
        if file_row is None:
            raise RuntimeError("File not found")
        project = workspace.get_project(file_row["project_id"])
        if project is None:
            raise RuntimeError("Transcript no longer exists")
        docs = _file_documents(
            file_row, project, languages_for(file_row["id"]), cancel, report, (0, 90)
        )
        stem = Path(file_row["rel_path"]).stem
        target = exports_dir(project) / export_name(stem, language, combine)

    report(95, "Erzeuge PDF ...")
    render_pdf(
        docs,
        project.get("type_structure") or "",
        target,
        keep_sections=bool(project.get("type_keep_sections")),
    )
    report(100, f"Export fertig: {target.name}")


def enqueue_file_export(
    file_row: dict[str, Any], language: str, session_id: str, combine: bool = False
) -> dict[str, Any]:
    return job_queue.enqueue(
        "export_pdf",
        payload={
            "scope": "file",
            "file_id": file_row["id"],
            "language": language,
            "combine": combine,
        },
        file_id=file_row["id"],
        project_id=file_row["project_id"],
        session_id=session_id,
    )


def enqueue_project_export(
    project_id: int, language: str, session_id: str, combine: bool = False
) -> dict[str, Any]:
    return job_queue.enqueue(
        "export_pdf",
        payload={
            "scope": "project",
            "project_id": project_id,
            "language": language,
            "combine": combine,
        },
        project_id=project_id,
        session_id=session_id,
    )
