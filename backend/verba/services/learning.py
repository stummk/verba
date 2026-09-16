"""The words a user keeps correcting, and what they correct them to.

Phase 2 of docs/LEARNING.md: the cheapest half of learning from corrections,
and the only one that needs no LLM at all. Phase 1 kept what a text looked
like before the user changed it; here that pair is read word by word.

What is read are the corrections of the *processed* text — the cleanup and
the translations. A segment is where somebody retypes what they think they
heard; the processed text is where they read carefully and fix a name, and
that is also where the rules of phase 3 come from, so there is one source to
reason about rather than two.

The transcript is the fallback, and only that: it teaches while the file has
no cleanup at all, because an installation without a language model never
gets one and would otherwise learn nothing — no spellings, and so no hotwords
for the next transcription. The moment a cleanup exists it falls silent
(`note_segment_correction`).

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
import threading
from collections.abc import Callable
from typing import Any

from .. import config, db
from ..core.jobs import JobCancelled, job_queue

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


def note_text_correction(
    file_row: dict[str, Any] | None, before: str, after: str, language: str = ""
) -> int:
    """Record what one correction of a processed text says; returns how many pairs.

    `language` is the language the corrected text is *in*: the recording's own
    for a cleanup, the target language for a translation. Empty falls back to
    the recording's, which is right for everything that is not a translation.

    Never raises on anything but a programming error: a user's edit must not
    fail because a word could not be remembered.
    """
    if file_row is None or not _settings().enabled:
        return 0
    pairs = word_pairs(before, after)
    if not pairs:
        return 0
    owner_id = scope_for_file(file_row)
    language = (language or file_row.get("language") or "").strip()
    with db.get_conn() as conn:
        for source, target in pairs:
            _record(conn, owner_id, language, source, target)
    return len(pairs)


def has_processed_text(file_id: int) -> bool:
    """Whether this file has a cleanup to learn from instead of its segments."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM derived_texts WHERE file_id = ? AND kind = 'cleanup' "
            "AND trim(content) != '' LIMIT 1",
            (file_id,),
        ).fetchone()
    return row is not None


def note_segment_correction(file_row: dict[str, Any] | None, before: str, after: str) -> int:
    """Record what one correction of the transcript says — where it is all there is.

    The processed text is the better source and is preferred wherever it
    exists: a segment is where somebody retypes what they think they heard,
    the cleanup is where they read carefully and fix a name. But an
    installation without a language model never has a cleanup, and without
    this it would learn nothing at all — no spellings, and so no hotwords for
    the next transcription, which is the most useful thing here.

    So the transcript teaches exactly while there is nothing better, and falls
    silent the moment a cleanup for the file exists. Nothing is counted twice
    that way: the cleanup is written *from* these segments, so a spelling
    already corrected here arrives in it correct.
    """
    if file_row is None or has_processed_text(file_row["id"]):
        return 0
    return note_text_correction(file_row, before, after)


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


def forget_scope(owner_id: int) -> dict[str, int]:
    """Drop everything one scope has learned; returns how much that was.

    Both callers mean it: the settings page, where somebody asked for it, and
    `auth.delete_user`, where the account that learned it is leaving. Nothing
    is held back at the installation scope either — the only way to make that
    safe would be a guard, and a guard here would quietly do nothing where a
    person asked for something.
    """
    with db.get_conn() as conn:
        terms = conn.execute("DELETE FROM learned_terms WHERE owner_id = ?", (owner_id,)).rowcount
        rules = conn.execute("DELETE FROM learned_rules WHERE owner_id = ?", (owner_id,)).rowcount
        for step in RULE_STEPS:
            conn.execute(
                "DELETE FROM meta WHERE key = ?", (f"{_RULE_RUN_MARKER}:{owner_id}:{step}",)
            )
    return {"terms": terms, "rules": rules}


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


# ── the rules distilled from corrected texts (phase 3) ────────────────
#
# A spelling is a word pair and needs no LLM. A *rule* — "keep the filler
# words", "do not invent headings" — is a generalisation over several
# corrections, and only a model can write one. What comes back is never
# applied on its own: it is proposed, and a person decides.
#
# The one thing that cannot be left to the prompt is content. A rule quoting
# a name out of somebody's transcript would carry that name into everybody
# else's prompt once the scopes are pooled, so every word of a proposed rule
# is weighed against the material: a word that lives in a single transcript
# is content, not a rule, and the proposal is dropped.

#: Which prompt a rule belongs to, and the derived text it is learned from.
RULE_STEPS = ("cleanup", "translate")
_TEXT_KIND = {"cleanup": "cleanup", "translate": "translation"}

_RULE_RUN_MARKER = "learning_rules_run"

DISTIL_SYSTEM_PROMPT = (
    "You are given passages of automatically produced transcripts together with the "
    "version a human corrected them into. Work out the general instructions that would "
    "have made the machine produce the corrected version straight away.\n"
    "Reply only with a JSON array, without commentary or Markdown fences:\n"
    '[{"rule": "...", "seen": 2}]\n'
    "- `rule` is one short imperative sentence about wording, punctuation, structure or "
    "formatting, in the language of the passages.\n"
    "- `seen` is in how many of the passages that instruction would have helped.\n"
    "Report only what several passages show. State no names, places, numbers, topics or "
    "any other content of the passages — an instruction that only makes sense for one "
    "recording is not a rule. Report nothing rather than guessing; an empty array is a "
    "valid answer."
)

NOTHING_TO_LEARN = "Aus den Korrekturen ließ sich keine allgemeine Regel ableiten"


def _norm_rule(text: str) -> str:
    return " ".join(text.split()).casefold()


# ── the material one distillation looks at ───────────────────────────


def _scope_clause(owner_id: int) -> tuple[str, list[Any]]:
    """Restrict to one scope's transcripts — nothing to restrict at scope 0."""
    if owner_id == INSTALLATION_SCOPE:
        return "", []
    return " AND p.owner_id = ?", [owner_id]


def corrected_texts(owner_id: int, step: str, since: str = "", limit: int = 0) -> list[dict]:
    """Corrected derived texts of one scope, most recently edited first.

    Only rows carrying both versions: `model_content` is written the first
    time a user changes the text (phase 1), so a row without it was never
    corrected and has nothing to compare.
    """
    clause, params = _scope_clause(owner_id)
    sql = (
        "SELECT d.file_id, d.model_content, d.content FROM derived_texts d "
        "JOIN files f ON f.id = d.file_id JOIN projects p ON p.id = f.project_id "
        "WHERE d.kind = ? AND d.edited_at != '' AND d.model_content != ''" + clause
    )
    args: list[Any] = [_TEXT_KIND[step], *params]
    if since:
        sql += " AND d.edited_at > ?"
        args.append(since)
    sql += " ORDER BY d.edited_at DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with db.get_conn() as conn:
        return db.rows_to_dicts(conn.execute(sql, args).fetchall())


_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def changed_passages(before: str, after: str, max_chars: int) -> list[tuple[str, str]]:
    """The paragraphs that came back changed, as (machine, human) pairs.

    Only replacements: a paragraph merely added or deleted has no counterpart
    to compare it with, and on its own it is content rather than an
    instruction.
    """
    old = [p.strip() for p in _PARAGRAPH_RE.split(before) if p.strip()]
    new = [p.strip() for p in _PARAGRAPH_RE.split(after) if p.strip()]
    pairs: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        was, now = "\n".join(old[i1:i2]), "\n".join(new[j1:j2])
        if was != now:
            pairs.append((was[:max_chars], now[:max_chars]))
    return pairs


def _examples(owner_id: int, step: str) -> tuple[list[tuple[str, str]], dict[str, set[int]]]:
    """The passages to learn from, and which files each of their words is in.

    The second half is what the content check runs on: a word spread over
    several transcripts is ordinary language, a word living in one of them is
    that transcript's content.
    """
    from . import llm

    settings = _settings()
    pairs: list[tuple[str, str]] = []
    corpus: dict[str, set[int]] = {}
    for row in corrected_texts(owner_id, step, limit=settings.max_examples):
        found = changed_passages(row["model_content"], row["content"], settings.max_example_chars)
        for word in llm.word_set(" ".join(part for pair in found for part in pair)):
            corpus.setdefault(word, set()).add(row["file_id"])
        pairs.extend(found)
        if len(pairs) >= settings.max_examples:
            break
    return pairs[: settings.max_examples], corpus


# ── weighing what came back ──────────────────────────────────────────


def is_content_free(text: str, corpus: dict[str, set[int]], min_files: int) -> bool:
    """Whether a proposed rule speaks about wording rather than about content.

    Weighed against the material, not against a word list: a word of the
    corrected texts that occurs in fewer than `min_files` of them belongs to
    those transcripts, and a rule quoting it would carry their content into
    every prompt the scope reaches. Words the material does not use at all are
    the model's own ordinary language and pass.
    """
    from . import llm

    return all(
        len(corpus.get(word, ())) >= min_files for word in llm.word_set(text) if word in corpus
    )


def proposals(answer: str, corpus: dict[str, set[int]]) -> list[tuple[str, int]]:
    """The usable rules of one answer: general enough, and content-free.

    `seen` is the model's own count of how many passages a rule holds for.
    It is a claim, not a proof — which is why it is only one of three things
    standing between an answer and a prompt: the content check below is
    measured against the material, and nothing applies before a person has
    accepted it.
    """
    from . import llm

    settings = _settings()
    rules: list[tuple[str, int]] = []
    for item in (entry for array in llm.json_arrays(answer) for entry in array):
        if not isinstance(item, dict):
            continue
        text = str(item.get("rule", "")).strip()
        try:
            seen = int(item.get("seen", 0))
        except (TypeError, ValueError):
            seen = 0
        if not text or seen < settings.min_rule_evidence:
            continue
        if not is_content_free(text, corpus, settings.min_rule_files):
            logger.info("dropped a distilled rule: it speaks of the material, not about it")
            continue
        rules.append((text, seen))
    return rules


# ── storing them ─────────────────────────────────────────────────────


def record_rule(owner_id: int, step: str, text: str, evidence: int) -> bool:
    """Store one proposal, or confirm the one already there.

    A scope keeps at most `max_rules` per step: a new rule displaces the
    weakest stored one, and only when it rests on more than that one does.
    Returns whether anything was written.
    """
    settings = _settings()
    norm = _norm_rule(text)
    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM learned_rules WHERE owner_id = ? AND step = ? AND norm = ?",
            (owner_id, step, norm),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE learned_rules SET evidence = ? WHERE id = ?",
                (max(existing["evidence"], evidence), existing["id"]),
            )
            return True
        rows = conn.execute(
            "SELECT id, evidence FROM learned_rules WHERE owner_id = ? AND step = ? "
            "ORDER BY evidence ASC, id ASC",
            (owner_id, step),
        ).fetchall()
        if len(rows) >= settings.max_rules:
            weakest = rows[0]
            if weakest["evidence"] >= evidence:
                return False  # the scope is full of better-attested rules
            conn.execute("DELETE FROM learned_rules WHERE id = ?", (weakest["id"],))
        conn.execute(
            "INSERT INTO learned_rules (owner_id, step, text, norm, evidence) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, step, text, norm, evidence),
        )
    return True


def list_rules(owner_id: int, step: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM learned_rules WHERE owner_id = ?"
    params: list[Any] = [owner_id]
    if step:
        sql += " AND step = ?"
        params.append(step)
    with db.get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY step, evidence DESC, id", params).fetchall()
    return db.rows_to_dicts(rows)


def get_rule(rule_id: int, owner_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM learned_rules WHERE id = ? AND owner_id = ?", (rule_id, owner_id)
        ).fetchone()
    return db.row_to_dict(row)


def update_rule(
    rule_id: int, owner_id: int, *, text: str | None = None, status: str | None = None
) -> dict[str, Any] | None:
    """Reword or judge one rule — only inside the caller's own scope."""
    sets: list[str] = []
    params: list[Any] = []
    if text is not None:
        sets.extend(["text = ?", "norm = ?"])
        params.extend([text, _norm_rule(text)])
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        sets.append("status = ?")
        params.append(status)
    if not sets:
        return get_rule(rule_id, owner_id)
    with db.get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE learned_rules SET {', '.join(sets)} "  # noqa: S608 — columns are literals
            "WHERE id = ? AND owner_id = ?",
            (*params, rule_id, owner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_rule(rule_id, owner_id)


def delete_rule(rule_id: int, owner_id: int) -> bool:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM learned_rules WHERE id = ? AND owner_id = ?", (rule_id, owner_id)
        )
        return cursor.rowcount > 0


# ── when a distillation is worth its calls ───────────────────────────


def _last_run(owner_id: int, step: str) -> str:
    with db.get_conn() as conn:
        return db.get_meta(conn, f"{_RULE_RUN_MARKER}:{owner_id}:{step}") or ""


def _mark_run(owner_id: int, step: str) -> None:
    with db.get_conn() as conn:
        now = conn.execute("SELECT datetime('now') AS now").fetchone()["now"]
        db.set_meta(conn, f"{_RULE_RUN_MARKER}:{owner_id}:{step}", now)


def request_rule_learning(file_row: dict[str, Any] | None, kind: str, session_id: str = "") -> None:
    """Queue a distillation once enough has been corrected to say anything.

    Deliberately not after every edit: it costs LLM calls and would hold the
    llm lane against the user's own work. One job per scope and step at a
    time — `has_active` is kind-wide and would let one scope block the other.
    """
    from . import llm

    settings = _settings()
    step = next((s for s, k in _TEXT_KIND.items() if k == kind), "")
    if not step or file_row is None or not settings.enabled or not settings.rules_enabled:
        return
    if llm.llm_location() == "none":
        return
    owner_id = scope_for_file(file_row)
    payload = {"owner_id": owner_id, "step": step}
    if any(job["payload"] == payload for job in job_queue.active_jobs("learn_rules")):
        return
    fresh = corrected_texts(owner_id, step, since=_last_run(owner_id, step))
    if len({row["file_id"] for row in fresh}) < settings.min_corrected_files:
        return
    job_queue.enqueue("learn_rules", payload=payload, session_id=session_id)


def handle_learn_rules_job(
    job: dict[str, Any], cancel: threading.Event, report: Callable[[int, str], None]
) -> str:
    """Job handler: read this scope's corrections and propose rules from them.

    Payload: {"owner_id": int, "step": "cleanup" | "translate"}
    """
    from . import llm

    payload = job["payload"]
    owner_id = int(payload["owner_id"])
    step = str(payload["step"])
    if step not in RULE_STEPS:
        raise RuntimeError(f"Unbekannter Lernschritt: {step}")

    report(5, "Korrekturen werden gesammelt")
    pairs, corpus = _examples(owner_id, step)
    if cancel.is_set():
        raise JobCancelled()
    if not pairs:
        _mark_run(owner_id, step)
        report(100, NOTHING_TO_LEARN)
        return NOTHING_TO_LEARN

    report(30, f"{len(pairs)} korrigierte Abschnitte werden ausgewertet")
    material = "\n\n".join(
        f"--- {index + 1} ---\nMaschine:\n{was}\nMensch:\n{now}"
        for index, (was, now) in enumerate(pairs)
    )
    answer = llm.chat(
        [
            {"role": "system", "content": DISTIL_SYSTEM_PROMPT},
            {"role": "user", "content": material},
        ]
    )
    if cancel.is_set():
        raise JobCancelled()

    report(80, "Regeln werden geprüft")
    written = sum(
        record_rule(owner_id, step, text, seen) for text, seen in proposals(answer, corpus)
    )
    _mark_run(owner_id, step)
    message = f"{written} Regel(n) vorgeschlagen" if written else NOTHING_TO_LEARN
    report(100, message)
    return message


# ── handing the rules to the steps (phase 4) ─────────────────────────


RULES_HEADING = "\n\nWhat earlier corrections of this kind of text asked for:\n"


def active_rules(owner_id: int, step: str) -> list[dict[str, Any]]:
    """The rules of one scope that actually apply.

    Only what somebody accepted. A proposal, however well attested, is not an
    instruction — that is the difference between a spelling, which is a fact
    about a word, and a rule, which is a judgement about a text.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM learned_rules WHERE owner_id = ? AND step = ? "
            "AND status = 'accepted' ORDER BY evidence DESC, id",
            (owner_id, step),
        ).fetchall()
    return db.rows_to_dicts(rows)


def rules_block(file_row: dict[str, Any] | None, step: str) -> tuple[str, list[int]]:
    """What a step is told about earlier corrections, and which rules that was.

    The ids come back with the text so the caller can write down what the run
    was given: without that, "why does this file read differently from that
    one" has no answer, and a correction afterwards has nothing to count
    against.

    Bounded, and the bound matters: every character here is one the section
    itself does not get in a small local model's context.
    """
    settings = _settings()
    if not settings.enabled or not settings.rules_enabled or file_row is None:
        return "", []
    rules = active_rules(scope_for_file(file_row), step)
    if not rules:
        return "", []
    lines: list[str] = []
    used: list[int] = []
    length = 0
    for rule in rules:
        line = f"- {rule['text']}"
        if length + len(line) + 1 > settings.max_glossary_chars:
            break
        lines.append(line)
        used.append(int(rule["id"]))
        length += len(line) + 1
    if not lines:
        return "", []
    return RULES_HEADING + "\n".join(lines), used


def note_rules_contradicted(rule_ids: list[int]) -> None:
    """A text written under these rules was corrected again — count that.

    Not proof that any one of them is wrong, which is why it takes as many
    corrections as the rule has evidence before it steps aside. But a rule
    whose texts keep being rewritten is not helping, and without this the
    first accepted rule would stand for good — the imprinting problem the
    spellings solve with the same arithmetic.
    """
    if not rule_ids:
        return
    marks = ", ".join("?" for _ in rule_ids)
    with db.get_conn() as conn:
        conn.execute(
            f"UPDATE learned_rules SET counter_evidence = counter_evidence + 1 "  # noqa: S608
            f"WHERE id IN ({marks})",
            rule_ids,
        )
        conn.execute(
            f"UPDATE learned_rules SET status = 'retired' "  # noqa: S608
            f"WHERE id IN ({marks}) AND status = 'accepted' AND counter_evidence > evidence",
            rule_ids,
        )
