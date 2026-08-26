"""Ask a question about the user guide and let the LLM answer from it.

One question, one answer — deliberately not a chat: every question starts
from a fresh context, which keeps the prompt small and the answer traceable
to the guide instead of to an earlier turn.

The guide is far larger than a small local model's context window, so the
question never gets the whole document: sections are ranked by how well they
match the question and only the best ones are packed into a character budget.
If the endpoint still complains about the context length, the budget is
halved and the call retried, so the user gets an answer instead of an error.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .. import config
from . import llm

logger = logging.getLogger(__name__)

SUPPORTED = ("de", "en", "ru")
DEFAULT_LANG = "de"

# Roughly 3k tokens of guide text — comfortable for a 4k-context local model
# and still enough for two or three complete sections.
CONTEXT_BUDGET_CHARS = 12000
MIN_BUDGET_CHARS = 2500
MAX_ANSWER_TOKENS = 800
# The whole guide fits into the budget, so the number of sections is what
# actually keeps a prompt focused: the best few, not everything that shares a
# word with the question.
MAX_SECTIONS = 6

# "## Title {#slug}" — the slug is stable across translations
HEADING = re.compile(r"^##\s+(?P<title>.*?)(?:\s*\{#(?P<slug>[a-z0-9-]+)\})?\s*$")
# Three characters, because the words that matter most here are short: PDF,
# LLM, API, Job, Tag, Ort.
WORD = re.compile(r"\w{3,}", re.UNICODE)
# Function words appear in every section and would only add noise to the
# ranking (de/en/ru, the guide's three languages).
_STOPWORD_TEXT = """
aber alle als auch auf aus bei bis das dass dem den der des die diese dieser doch dort ein
eine einen einer eines für hier ich ihr ist kann man mich mir mit nach nicht noch nur oder
sich sie soll und unter vom von war was wenn wer wie wird zum zur über
all and any are but can does for from has have how into its not the that this what when
where which who why with you your
для его или как когда мне над них она они при так там что чем это эти этот
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

SYSTEM_PROMPT = (
    "You are the help assistant of the audio transcription application Verba. "
    "Answer the user's question using only the excerpt of the user guide below. "
    "Do not invent features, settings or menu entries that the excerpt does not "
    "mention. If the excerpt does not cover the question, say so plainly and name "
    "the section that comes closest. Answer in the language of the question, "
    "briefly and concretely, in at most three short paragraphs; steps as a list."
)


@dataclass
class Section:
    slug: str
    title: str
    body: str

    @property
    def text(self) -> str:
        return f"## {self.title}\n{self.body}"


def docs_dir() -> Path:
    return config.bundle_root() / "docs" / "user"


def guide_path(lang: str) -> Path:
    """Path of the guide for a language, falling back to the default."""
    selected = lang if lang in SUPPORTED else DEFAULT_LANG
    path = docs_dir() / f"{selected}.md"
    return path if path.exists() else docs_dir() / f"{DEFAULT_LANG}.md"


def available() -> bool:
    """Whether questions can be answered at all — needs a configured LLM."""
    return llm.llm_location() != "none"


def split_sections(markdown: str) -> tuple[str, list[Section]]:
    """Split the guide into the intro and one Section per "## " heading."""
    intro: list[str] = []
    collected: list[tuple[str, str, list[str]]] = []
    current: list[str] | None = None
    fence = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        match = None if fence else HEADING.match(line)
        if match:
            current = []
            collected.append((match.group("slug") or "", match.group("title").strip(), current))
            continue
        (current if current is not None else intro).append(line)
    sections = [
        Section(slug=slug, title=title, body="\n".join(lines).strip())
        for slug, title, lines in collected
    ]
    return "\n".join(intro).strip(), sections


def _score(section: Section, tokens: list[str]) -> float:
    """How well a section matches the question.

    Substring counting instead of exact word matching: German inflections and
    compounds ("Übersetzungen" vs "Übersetzung") should still count.
    """
    title = section.title.lower()
    body = section.body.lower()
    score = 0.0
    for token in tokens:
        score += 5.0 * title.count(token)
        hits = body.count(token)
        if hits:
            # diminishing returns, so one chatty section cannot crowd out
            # a section that matches several different words of the question
            score += 1.0 + 0.2 * min(hits - 1, 10)
    return score


def _truncate(body: str, limit: int) -> str:
    """Cut a section at a paragraph boundary so it stays readable."""
    if len(body) <= limit:
        return body
    cut = body[:limit]
    boundary = cut.rfind("\n\n")
    if boundary > limit // 3:
        cut = cut[:boundary]
    return f"{cut.rstrip()}\n\n[…]"


def select_context(
    question: str, sections: list[Section], budget: int = CONTEXT_BUDGET_CHARS
) -> tuple[list[Section], bool]:
    """Pick the sections that go into the prompt.

    Only sections that actually match the question are used — filling the rest
    of the budget with unrelated ones costs tokens and makes a small model
    wander. If nothing matches (wording the guide does not use), the guide is
    taken in document order instead of sending no context at all.

    Returns them in document order together with a flag telling whether
    anything had to be left out or shortened.
    """
    words = {token.lower() for token in WORD.findall(question)}
    tokens = sorted(words - STOPWORDS, key=len, reverse=True)
    scored = [(_score(section, tokens), index) for index, section in enumerate(sections)]
    matching = [(score, index) for score, index in scored if score > 0]
    ranked = [index for _, index in sorted(matching or scored, key=lambda p: (-p[0], p[1]))]
    ranked = ranked[:MAX_SECTIONS]

    chosen: dict[int, Section] = {}
    remaining = budget
    truncated = len(ranked) < len(matching or scored)
    for index in ranked:
        section = sections[index]
        if remaining <= 0:
            truncated = True
            break
        if len(section.text) <= remaining:
            chosen[index] = section
            remaining -= len(section.text)
            continue
        if not chosen:  # guarantee at least one section, shortened if need be
            room = max(remaining - len(section.title) - 4, 200)
            chosen[index] = Section(section.slug, section.title, _truncate(section.body, room))
            remaining = 0
        truncated = True
    return [chosen[index] for index in sorted(chosen)], truncated


def _is_context_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in ("context", "too long", "too many tokens", "maximum length", "n_ctx")
    )


def ask(question: str, lang: str = DEFAULT_LANG) -> dict:
    """Answer one question about the guide; raises LLMError on a dead endpoint."""
    markdown = guide_path(lang).read_text(encoding="utf-8")
    intro, sections = split_sections(markdown)

    budget = CONTEXT_BUDGET_CHARS
    last_error: Exception | None = None
    while budget >= MIN_BUDGET_CHARS:
        chosen, truncated = select_context(question, sections, budget)
        prompt = "\n\n".join([intro[:800], *(section.text for section in chosen)])
        try:
            answer = llm.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{prompt}\n\n---\n\n{question}"},
                ],
                temperature=0.1,
                max_tokens=MAX_ANSWER_TOKENS,
            )
        except llm.LLMError as exc:
            last_error = exc
            if not _is_context_error(exc):
                raise
            budget //= 2
            logger.warning("docs question hit the context limit, retrying with %d chars", budget)
            continue
        return {
            "answer": answer.strip(),
            "truncated": truncated,
            "sections": [{"slug": s.slug, "title": s.title} for s in chosen],
        }

    raise llm.LLMError(
        "Die Frage konnte nicht beantwortet werden — das Sprachmodell hat zu wenig "
        f"Kontext dafür: {last_error}"
    )
