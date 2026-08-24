"""PDF export: two-stage template pipeline.

Stage 1 (structure): turn the best available text of a file into a neutral
block structure — via the configured LLM (strict JSON output) or, without
one, rule-based from paragraphs/segments. Export therefore always works;
with an LLM it gets smarter (stanzas, dialogue roles, minutes with to-dos).

Stage 2 (render): a deterministic fpdf2 renderer lays the blocks out
according to the transcript type's template. Folder exports append each
file as a section separated by spacing only — no table of contents, no
extra section titles; the per-file header comes from file metadata.
"""

from __future__ import annotations

import json
import logging
import platform
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.jobs import JobCancelled, job_queue
from . import llm, pipeline, transcripts, workspace
from .metadata import format_display_date

logger = logging.getLogger(__name__)

# Types whose text is spoken by several roles — structured from segments
# (which carry speakers) instead of the flattened cleanup text.
DIALOGUE_TYPES = {"interview", "roleplay"}
STANZA_TYPES = {"song", "poem"}

BLOCK_KINDS = {"heading", "paragraph", "stanza", "dialogue", "list", "separator"}

STRUCTURE_SYSTEM_PROMPT = (
    "You structure a transcript for PDF export. Reply only with a JSON array of blocks "
    "without commentary or Markdown fences. Allowed blocks:\n"
    '{"kind": "heading", "text": "..."} - subheading\n'
    '{"kind": "paragraph", "text": "..."} - paragraph\n'
    '{"kind": "stanza", "lines": ["..."]} - stanza (poem/song), one line at a time\n'
    '{"kind": "dialogue", "speaker": "...", "text": "..."} - spoken contribution\n'
    '{"kind": "list", "title": "...", "items": ["..."]} - list (e.g. decisions, to-dos)\n'
    '{"kind": "separator"} - separator between sections\n'
    "Reproduce the full text; do not summarize or omit anything unless the transcript "
    "type context explicitly requires it."
)


# ── stage 1: structure ────────────────────────────────────────────────


def _base_text(file_id: int, type_key: str, language: str) -> str:
    """The text a structure is built from, best variant first."""
    if language:
        text = pipeline.get_text(file_id, "translation", language)
        if text is None:
            raise RuntimeError(f"No translation ({language}) available")
        return text["content"]
    if type_key in DIALOGUE_TYPES:
        segments = transcripts.list_segments(file_id)
        if segments:
            lines = []
            for segment in segments:
                speaker = (segment.get("speaker") or "").strip()
                text = segment["text"].strip()
                lines.append(f"{speaker}: {text}" if speaker else text)
            return "\n".join(lines)
    cleanup = pipeline.get_text(file_id, "cleanup")
    if cleanup is not None:
        return cleanup["content"]
    segments = transcripts.list_segments(file_id)
    if not segments:
        raise RuntimeError("No segments — transcribe the file first")
    return "\n".join(s["text"].strip() for s in segments)


def _parse_blocks(raw: str) -> list[dict[str, Any]] | None:
    """Extract and validate the JSON block array from an LLM answer."""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    blocks: list[dict[str, Any]] = []
    for item in data:
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


def _structure_rule_based(text: str, type_key: str) -> list[dict[str, Any]]:
    """Deterministic fallback: paragraphs, stanzas or speaker turns from plain text."""
    type_key = {"lied": "song", "gedicht": "poem", "protokoll": "protocol"}.get(type_key, type_key)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if type_key in STANZA_TYPES:
        return [
            {"kind": "stanza", "lines": [line.strip() for line in p.splitlines() if line.strip()]}
            for p in paragraphs
        ]
    if type_key in DIALOGUE_TYPES:
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


def _structure_llm(
    text: str,
    type_prompt: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int],
) -> list[dict[str, Any]] | None:
    system_prompt = STRUCTURE_SYSTEM_PROMPT
    if type_prompt:
        system_prompt += "\n\nContext of the transcription type:\n" + type_prompt
    chunks = pipeline._chunk_text(text)
    blocks: list[dict[str, Any]] = []
    lo, hi = progress_range
    for i, chunk in enumerate(chunks):
        if cancel.is_set():
            raise JobCancelled()
        report(lo + (hi - lo) * i // max(1, len(chunks)), f"Structuring {i + 1}/{len(chunks)}")
        answer = llm.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": chunk},
            ]
        )
        parsed = _parse_blocks(answer)
        if parsed is None:
            logger.warning("structuring response not parseable — rule-based fallback")
            return None
        blocks.extend(parsed)
    return blocks


def build_document(
    file_row: dict[str, Any],
    project: dict[str, Any],
    language: str,
    cancel: threading.Event,
    report: Callable[[int, str], None],
    progress_range: tuple[int, int] = (0, 100),
) -> dict[str, Any]:
    """Stage 1 for one file: title/date from metadata plus structured blocks."""
    type_key = project.get("type_key") or ""
    text = _base_text(file_row["id"], type_key, language)

    blocks: list[dict[str, Any]] | None = None
    if type_key and llm.llm_location() != "none":
        blocks = _structure_llm(
            text, project.get("type_prompt") or "", cancel, report, progress_range
        )
    if blocks is None:
        blocks = _structure_rule_based(text, type_key)

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


class _Renderer:
    """Deterministic layout of structure blocks according to the type template."""

    def __init__(self, pdf: Any, family: str, type_key: str) -> None:
        self.pdf = pdf
        self.family = family
        self.type_key = type_key

    def _text(self, value: str) -> str:
        return _sanitize_for(self.family, value)

    def _write(self, height: float, value: str) -> None:
        """Full-width wrapped text; cursor continues at the left margin."""
        self.pdf.multi_cell(0, height, self._text(value), new_x="LMARGIN", new_y="NEXT")

    def section(self, doc: dict[str, Any]) -> None:
        """Render one file's optional header and its blocks."""
        pdf = self.pdf
        left = doc.get("header_left") or ""
        middle = doc.get("header_middle") or ""
        right = doc.get("header_right") or ""
        if left or middle or right:
            page_width = pdf.w - pdf.l_margin - pdf.r_margin
            pdf.set_font(self.family, "B", 11)
            pdf.cell(page_width * 0.45, 7, self._text(left), align="L")
            pdf.set_font(self.family, "", 11)
            pdf.cell(
                page_width * 0.30,
                7,
                self._text(f"({middle})" if middle else ""),
                align="C",
            )
            pdf.set_font(self.family, "B", 11)
            pdf.cell(
                page_width * 0.25,
                7,
                self._text(right),
                align="R",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            pdf.ln(4)
        for block in doc["blocks"]:
            self._block(block)

    def _block(self, block: dict[str, Any]) -> None:
        pdf = self.pdf
        kind = block["kind"]
        if kind == "heading":
            pdf.ln(2)
            pdf.set_font(self.family, "B", 12)
            self._write(7, block["text"])
            pdf.ln(1)
        elif kind == "stanza":
            pdf.set_font(self.family, "", 11)
            for line in block["lines"]:
                self._write(6, line)
            pdf.ln(4)
        elif kind == "dialogue":
            speaker = block.get("speaker") or ""
            if self.type_key == "roleplay":
                speaker = speaker.upper()
            if speaker:
                pdf.set_font(self.family, "B", 11)
                self._write(6, speaker)
            pdf.set_font(self.family, "", 11)
            self._write(6, block["text"])
            pdf.ln(2)
        elif kind == "list":
            if block.get("title"):
                pdf.set_font(self.family, "B", 11)
                self._write(6, block["title"])
            pdf.set_font(self.family, "", 11)
            for item in block["items"]:
                self._write(6, f"•  {item}")
            pdf.ln(3)
        elif kind == "separator":
            pdf.set_font(self.family, "", 11)
            pdf.cell(0, 8, self._text("*   *   *"), align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(4)
        else:  # paragraph
            pdf.set_font(self.family, "", 11)
            self._write(6, block["text"])
            pdf.ln(3)


def render_pdf(docs: list[dict[str, Any]], type_key: str, target: Path) -> None:
    """Stage 2: render one or many file sections into a single PDF.

    Multiple sections flow continuously, separated by spacing only — no table
    of contents and no extra section titles (the template header per file is
    the only marker).
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
    renderer = _Renderer(pdf, family, type_key)
    for index, doc in enumerate(docs):
        if index:
            pdf.ln(14)
        renderer.section(doc)
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(target))


# ── job handler & helpers ─────────────────────────────────────────────


def exports_dir(project: dict[str, Any]) -> Path:
    return workspace.project_dir(project) / "exports"


def export_name(stem: str, language: str) -> str:
    return f"{stem}.{language}.pdf" if language else f"{stem}.pdf"


def list_exports(project: dict[str, Any]) -> list[dict[str, Any]]:
    directory = exports_dir(project)
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*.pdf")):
        stat = path.stat()
        entries.append({"name": path.name, "size": stat.st_size, "modified": int(stat.st_mtime)})
    return entries


def handle_export_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> None:
    """Job handler: export one file or a whole project as PDF.

    Payload: {"scope": "file"|"project", "file_id"?, "project_id"?, "language": ""}
    """
    payload = job["payload"]
    scope = payload.get("scope", "file")
    language = payload.get("language", "")

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
            report(lo, f"File {index + 1}/{len(files)}")
            docs.append(build_document(file_row, project, language, cancel, report, (lo, hi)))
        target = exports_dir(project) / export_name(project["slug"], language)
    else:
        file_row = workspace.get_file(int(payload["file_id"]))
        if file_row is None:
            raise RuntimeError("File not found")
        project = workspace.get_project(file_row["project_id"])
        if project is None:
            raise RuntimeError("Transcript no longer exists")
        docs = [build_document(file_row, project, language, cancel, report, (0, 90))]
        target = exports_dir(project) / export_name(Path(file_row["rel_path"]).stem, language)

    report(95, "Generating PDF")
    render_pdf(docs, project.get("type_key") or "", target)
    report(100, f"Export fertig: {target.name}")


def enqueue_file_export(file_row: dict[str, Any], language: str, session_id: str) -> dict[str, Any]:
    return job_queue.enqueue(
        "export_pdf",
        payload={"scope": "file", "file_id": file_row["id"], "language": language},
        file_id=file_row["id"],
        project_id=file_row["project_id"],
        session_id=session_id,
    )


def enqueue_project_export(project_id: int, language: str, session_id: str) -> dict[str, Any]:
    return job_queue.enqueue(
        "export_pdf",
        payload={"scope": "project", "project_id": project_id, "language": language},
        project_id=project_id,
        session_id=session_id,
    )
