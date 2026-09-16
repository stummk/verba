"""The words a user keeps correcting, and what they correct them to.

Phase 2 of docs/LEARNING.md: the cheapest half of learning from corrections,
and the only one that needs no LLM at all. Phase 1 kept what a text looked
like before the user changed it; here that pair is read word by word.

Only 1:1 word replacements are taken, and only where the corrected word still
*looks* like the one it replaces. A mishearing does — "Jahwe" → "Yahweh",
"Bergstrasse" → "Bergstraße" — and is worth remembering. A rewording does not
— "sagt" → "spricht" — and as a hint to the recognition it would be nonsense.
Sentence rewrites are ignored here entirely; turning those into rules is what
the LLM step of phase 3 is for.

What is learned is applied in two places, and the first is the reason this
phase is worth its trouble:

- the transcription itself (`services/whisper.py`), where the learned
  spellings go in as `hotwords` — the name is then *heard* correctly next
  time instead of being repaired afterwards by an LLM;
- the glossary the cleanup and the translation are given
  (`services/pipeline.py`), which is what keeps a spelling stable across
  files rather than only across the chunks of one.

Scope. Everything carries an `owner_id`, and 0 means the whole installation —
which is what every setup without user management has, so there "global" is
simply global. With user management on, each user learns across their own
transcripts, because a spelling out of a private transcript would otherwise
stand in everybody's prompt; `settings.learning.shared` is the deliberate
decision to pool them anyway.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from .. import config, db

logger = logging.getLogger(__name__)

INSTALLATION_SCOPE = 0

#: A word for this purpose: letters only, hyphens and apostrophes inside it.
#: Digits are deliberately out — a corrected number is not a spelling.
_WORD_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)

#: Below this a replacement says nothing: "de" → "da" is noise, not a name.
MIN_WORD_CHARS = 3

#: How many pairs one edit may contribute. Somebody who retypes a whole
#: segment is rewriting it, not correcting its spelling, and a rewrite must
#: not dump its whole vocabulary into the store.
MAX_PAIRS_PER_EDIT = 5

STATUSES = ("proposed", "accepted", "rejected", "retired")

# A term counts while the user has accepted it, or while it has been seen
# often enough and nobody has said no. "rejected" is sticky on purpose: more
# evidence must not overrule a person.
_ACTIVE_SQL = "(status = 'accepted' OR (status = 'proposed' AND evidence >= ?))"


def _settings() -> config.LearningSettings:
    return config.get_settings().learning


# ── which scope a transcript's corrections count for ──────────────────


def scope_for_project(project: dict[str, Any] | None) -> int:
    """The owner the corrections on this project's files are learned for."""
    from . import auth

    if not auth.enabled() or _settings().shared:
        return INSTALLATION_SCOPE
    return int((project or {}).get("owner_id") or INSTALLATION_SCOPE)


def scope_for_file(file_row: dict[str, Any] | None) -> int:
    from . import workspace

    if file_row is None:
        return INSTALLATION_SCOPE
    return scope_for_project(workspace.get_project(file_row["project_id"]))


def scope_for_user(user: dict[str, Any] | None) -> int:
    """The scope a request acts in — what the API reads and writes."""
    from . import auth

    if not auth.enabled() or _settings().shared:
        return INSTALLATION_SCOPE
    return int((user or {}).get("id") or INSTALLATION_SCOPE)


# ── reading one correction ────────────────────────────────────────────


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def _worth_keeping(source: str, target: str, min_similarity: float) -> bool:
    if source == target:
        return False
    if len(source) < MIN_WORD_CHARS or len(target) < MIN_WORD_CHARS:
        return False
    ratio = difflib.SequenceMatcher(None, source.casefold(), target.casefold()).ratio()
    return ratio >= min_similarity


def word_pairs(
    before: str, after: str, min_similarity: float | None = None
) -> list[tuple[str, str]]:
    """The single-word replacements between two versions of the same text.

    Pure by design, so what counts as a correction can be tested without a
    database. Only `replace` opcodes of exactly one word against exactly one
    word are taken: an inserted or deleted word says nothing about spelling,
    and a block of several is a rewrite.
    """
    if min_similarity is None:
        min_similarity = _settings().min_similarity
    old, new = _words(before), _words(after)
    if not old or not new:
        return []
    pairs: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
            continue
        source, target = old[i1], new[j1]
        if _worth_keeping(source, target, min_similarity):
            pairs.append((source, target))
    return pairs[:MAX_PAIRS_PER_EDIT]


# ── writing it down ───────────────────────────────────────────────────


def note_text_correction(file_row: dict[str, Any] | None, before: str, after: str) -> int:
    """Record what one correction of a transcript says; returns how many pairs.

    Never raises on anything but a programming error: a user's edit must not
    fail because a word could not be remembered.
    """
    if file_row is None or not _settings().enabled:
        return 0
    pairs = word_pairs(before, after)
    if not pairs:
        return 0
    owner_id = scope_for_file(file_row)
    language = (file_row.get("language") or "").strip()
    with db.get_conn() as conn:
        for source, target in pairs:
            _record(conn, owner_id, language, source, target)
    return len(pairs)


def _record(conn, owner_id: int, language: str, source: str, target: str) -> None:
    """One correction against what is already known about this word."""
    row = conn.execute(
        "SELECT * FROM learned_terms WHERE owner_id = ? AND language = ? AND source = ?",
        (owner_id, language, source),
    ).fetchone()

    if row is None:
        # Nothing about this word yet — but there may be a term claiming the
        # opposite direction, which the user has just turned back around.
        opposite = conn.execute(
            "SELECT * FROM learned_terms WHERE owner_id = ? AND language = ? "
            "AND source = ? AND target = ?",
            (owner_id, language, target, source),
        ).fetchone()
        if opposite is not None:
            _contradict(conn, opposite)
            return
        conn.execute(
            "INSERT INTO learned_terms (owner_id, language, source, target) VALUES (?, ?, ?, ?)",
            (owner_id, language, source, target),
        )
        return

    if row["target"] == target:
        # Confirmed. A term that was retired for being contradicted comes back
        # once it is the better attested of the two again.
        status = "proposed" if row["status"] == "retired" else row["status"]
        conn.execute(
            "UPDATE learned_terms SET evidence = evidence + 1, status = ?, "
            "last_seen_at = datetime('now') WHERE id = ?",
            (status, row["id"]),
        )
        return

    _contradict(conn, row, replacement=target)


def _contradict(conn, row: dict[str, Any], replacement: str = "") -> None:
    """The stored correction was written against; count that, and give way.

    Counting is what keeps a wrong spelling picked up from the first few
    recordings from being the installation's truth for good. Once it has been
    contradicted as often as it was confirmed, the newer wording takes over —
    or, where there is none to take over, the term steps aside.
    """
    counter = row["counter_evidence"] + 1
    if counter < row["evidence"]:
        conn.execute(
            "UPDATE learned_terms SET counter_evidence = ? WHERE id = ?", (counter, row["id"])
        )
        return
    if replacement:
        conn.execute(
            "UPDATE learned_terms SET target = ?, evidence = 1, counter_evidence = 0, "
            "status = 'proposed', last_seen_at = datetime('now') WHERE id = ?",
            (replacement, row["id"]),
        )
        return
    conn.execute(
        "UPDATE learned_terms SET counter_evidence = ?, status = 'retired' WHERE id = ?",
        (counter, row["id"]),
    )


# ── reading it back ───────────────────────────────────────────────────


def list_terms(owner_id: int) -> list[dict[str, Any]]:
    """Everything learned in one scope, best attested first — for the UI."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM learned_terms WHERE owner_id = ? "
            "ORDER BY evidence DESC, last_seen_at DESC, source",
            (owner_id,),
        ).fetchall()
    return db.rows_to_dicts(rows)


def active_terms(owner_id: int, language: str = "", limit: int = 0) -> list[dict[str, Any]]:
    """The terms that actually apply, best attested first.

    An empty `language` means the language is not known — at transcription
    time it often is not, because Whisper is about to detect it. Everything
    is then eligible, which the caller's cap keeps in check.
    """
    sql = f"SELECT * FROM learned_terms WHERE owner_id = ? AND {_ACTIVE_SQL}"
    params: list[Any] = [owner_id, _settings().min_evidence]
    if language:
        # a term from a recording whose language nobody recorded still counts
        sql += " AND (language = ? OR language = '')"
        params.append(language)
    sql += " ORDER BY evidence DESC, last_seen_at DESC, source"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with db.get_conn() as conn:
        return db.rows_to_dicts(conn.execute(sql, params).fetchall())


def update_term(
    term_id: int, owner_id: int, *, target: str | None = None, status: str | None = None
) -> dict[str, Any] | None:
    """Correct or judge one entry — only inside the caller's own scope."""
    sets: list[str] = []
    params: list[Any] = []
    if target is not None:
        sets.append("target = ?")
        params.append(target)
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        sets.append("status = ?")
        params.append(status)
    if not sets:
        return get_term(term_id, owner_id)
    with db.get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE learned_terms SET {', '.join(sets)} "  # noqa: S608 — columns are literals
            "WHERE id = ? AND owner_id = ?",
            (*params, term_id, owner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_term(term_id, owner_id)


def get_term(term_id: int, owner_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM learned_terms WHERE id = ? AND owner_id = ?", (term_id, owner_id)
        ).fetchone()
    return db.row_to_dict(row)


def delete_term(term_id: int, owner_id: int) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM learned_terms WHERE id = ? AND owner_id = ?", (term_id, owner_id)
        )
        return cursor.rowcount > 0


def forget_owner(owner_id: int) -> int:
    """Drop a scope's vocabulary — when its account goes, or on request."""
    if owner_id == INSTALLATION_SCOPE:
        return 0
    with db.get_conn() as conn:
        return conn.execute("DELETE FROM learned_terms WHERE owner_id = ?", (owner_id,)).rowcount


# ── handing it to the engines ─────────────────────────────────────────


def hotwords_for(file_row: dict[str, Any] | None, language: str = "") -> str:
    """The learned spellings as faster-whisper takes them, or "".

    Capped, and that cap is not a detail: an unbounded list of names bends the
    decoder towards words that do not occur in this recording and makes the
    transcription worse than it would have been without any of this.
    """
    settings = _settings()
    if not settings.enabled or not settings.max_hotwords or file_row is None:
        return ""
    terms = active_terms(scope_for_file(file_row), language, limit=settings.max_hotwords)
    return ", ".join(dict.fromkeys(term["target"] for term in terms))


def glossary_block(file_row: dict[str, Any] | None, language: str = "") -> str:
    """What the LLM steps are told about spellings — "" when nothing is known.

    Appended to the overview's own glossary, which states the names *this*
    recording uses; this one states the ones the user has settled on across
    recordings, which is the part a single file can never know.
    """
    settings = _settings()
    if not settings.enabled or not settings.max_glossary_chars or file_row is None:
        return ""
    terms = active_terms(scope_for_file(file_row), language or (file_row.get("language") or ""))
    if not terms:
        return ""
    entries: list[str] = []
    length = 0
    for term in terms:
        entry = f'"{term["target"]}" (not "{term["source"]}")'
        if length + len(entry) + 2 > settings.max_glossary_chars:
            break
        entries.append(entry)
        length += len(entry) + 2
    if not entries:
        return ""
    return "\nSpellings that have been corrected before, use them: " + ", ".join(entries) + "."
