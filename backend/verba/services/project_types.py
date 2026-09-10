"""Project types: named presets with two editable LLM prompts.

- `system_prompt` (cleanup): how the transcription itself is edited.
- `output_prompt` (output format): how the cleaned text is turned into
  document blocks for the PDF export. Empty means the default from
  `pdf.DEFAULT_OUTPUT_PROMPT` applies, so the export always works; new types
  are pre-filled with it so it can be adapted instead of guessed at.

`verbatim` decides whether that second prompt is used at all: with it on —
the default, and every builtin but the minutes type — the export structures
the cleaned text and the translations deterministically and reproduces them
word for word. It says nothing about the aufbereitung: what the export may do
with a text and how that text came about are two questions, and a type that
has its recording rewritten into a document may well want that document in
the PDF word for word.

`condense` is the other one: with it on, the cleanup prompt runs once over the
whole recording and writes a document of its own (minutes with decisions and
to-dos) instead of cleaning the transcript section by section. Off by default,
because reproducing the material is what a transcript type normally does.

`diarize` is the third, and the only one that acts before the LLM ever sees
the text: with it on, the transcription is followed by the speaker recognition
(services/diarize.py) — the segments get their speaker, and a segment in which
the speaker changes is split at that moment. On for the types that are a
conversation (interview, minutes, roleplay), off for the ones that are one
voice, where every recognised "second speaker" would be an error.

Seven builtin types ship with the app. They are seeded exactly once (tracked
via the meta table) so that deleting a builtin type sticks across restarts;
a "restore defaults" action re-inserts any missing builtin.

Projects without a type always work: plain transcription, plain-text PDF
export without template or structuring (phase 6).
"""

from __future__ import annotations

from typing import Any

from .. import db

SEED_MARKER = "project_types_seeded"
OUTPUT_PROMPT_MARKER = "project_types_output_prompts"
STRUCTURE_MARKER = "project_types_structures"
VERBATIM_MARKER = "project_types_verbatim"
CONDENSE_MARKER = "project_types_condense"
DIARIZE_MARKER = "project_types_diarize"

# The builtin output prompts embed the block contract; it is expanded into the
# stored prompt so nothing about the export stays hidden from the user.
CONTRACT_PLACEHOLDER = "{contract}"


BUILTIN_TYPES: list[dict[str, Any]] = [
    {
        "key": "song",
        "name": "Song",
        "structure": "stanzas",
        "verbatim": True,
        "condense": False,
        "diarize": False,
        "system_prompt": (
            "You are editing a Song transcription. Preserve line breaks and recognizable "
            "repetitions (choruses). Structure the text into verses and choruses, correct "
            "obvious mishearings carefully, and do not invent any lines. Keep every Refrain."
        ),
        "output_prompt": (
            "You lay out a song for PDF export. Emit every verse and every chorus as its "
            'own "stanza" block with one line per sung line; never merge lines into '
            'paragraphs. Put a "heading" block before a part only if the transcript names '
            'it (e.g. "Chorus", "Bridge"). {contract}\n'
            "Reproduce every line; do not summarize and do not drop repetitions."
        ),
    },
    {
        "key": "interview",
        "name": "Interview/Dialogue",
        "structure": "dialogue",
        "verbatim": True,
        "condense": False,
        "diarize": True,
        "system_prompt": (
            "You are editing an Interview or dialogue transcription. Assign spoken "
            "contributions to speakers (Speaker 1, Speaker 2, ... or recognized names), "
            "remove filler words and false starts, but preserve the wording and meaning "
            "of the statements. Mark unclear passages with [inaudible]."
        ),
        "output_prompt": (
            "You lay out an interview for PDF export. Emit every spoken contribution as a "
            '"dialogue" block with the speaker in the "speaker" field — never inside the '
            "text. Consecutive contributions by the same speaker stay separate blocks. "
            "{contract}\nReproduce the full conversation; do not summarize."
        ),
    },
    {
        "key": "speech",
        "name": "Speech",
        "structure": "paragraphs",
        "verbatim": True,
        "condense": False,
        "diarize": False,
        "system_prompt": (
            "You are editing a speech transcription. Divide the text into meaningful "
            "paragraphs, correct grammar and punctuation, and remove filler words. Keep "
            "quotations verbatim and mark them as quotations."
        ),
        "output_prompt": (
            "You lay out a speech for PDF export. Emit coherent sections as "
            '"paragraph" blocks and give a section a "heading" block when it clearly '
            "starts a new topic. {contract}\nReproduce the full text; do not summarize."
        ),
    },
    {
        "key": "protocol",
        "name": "Meeting Protocol",
        "structure": "paragraphs",
        "verbatim": False,
        "condense": True,
        "diarize": True,
        "system_prompt": (
            "You create meeting minutes from a conversation transcription. Summarize the "
            "discussion objectively, list decisions separately, and extract all tasks into "
            "a to-do list (who, what, by when, where stated). Strictly stay within the "
            "conversation's content."
        ),
        "output_prompt": (
            "You lay out meeting minutes for PDF export. Order: the discussion as "
            '"paragraph" blocks under "heading" blocks per topic, then a "list" block '
            'titled "Decisions" and a "list" block titled "To-dos" (one item per task, '
            "with who and by when where stated). Omit a list when the transcript holds "
            "nothing for it. {contract}\n"
            "Only ever use content from the conversation; invent nothing."
        ),
    },
    {
        "key": "poem",
        "name": "Poem",
        "structure": "stanzas",
        "verbatim": True,
        "condense": False,
        "diarize": False,
        "system_prompt": (
            "You are editing a poem transcription. Restore the stanza and verse structure, "
            "preserving rhythm, rhyme, and line breaks. Correct only unambiguous mishearings "
            "and do not change poetic wording."
        ),
        "output_prompt": (
            "You lay out a poem for PDF export. Emit every stanza as its own "
            '"stanza" block with one line per verse, keeping the line breaks exactly as '
            "they are. Never turn verses into paragraphs. {contract}\n"
            "Reproduce every line verbatim."
        ),
    },
    {
        "key": "roleplay",
        "name": "Roleplay",
        "structure": "script",
        "verbatim": True,
        "condense": False,
        "diarize": True,
        "system_prompt": (
            "You are editing a roleplay or play transcription. Turn the text into a script: "
            "put character names before each spoken contribution and italicize stage "
            "directions in parentheses. Assign contributions to roles based on voice and "
            "context; mark unclear assignments with (?)."
        ),
        "output_prompt": (
            "You lay out a script for PDF export. Emit every line of dialogue as a "
            '"dialogue" block with the character in the "speaker" field. Stage directions '
            'become "paragraph" blocks in parentheses, scene changes a "separator" block. '
            "{contract}\nReproduce the full script; do not summarize."
        ),
    },
]


LEGACY_KEYS = {
    "lied": "song",
    "protokoll": "protocol",
    "gedicht": "poem",
    "rollenspiel": "roleplay",
}


# The columns a joined type contributes to a project or a file row. Both rows
# carry them under the same names, which is what lets `for_file()` answer with
# one of the two without anybody downstream having to know which.
TYPE_FIELDS = (
    "type_id",
    "type_key",
    "type_name",
    "type_prompt",
    "type_output_prompt",
    "type_structure",
    "type_keep_sections",
    "type_verbatim",
    "type_condense",
    "type_diarize",
)


def for_file(project: dict[str, Any] | None, file_row: dict[str, Any] | None) -> dict[str, Any]:
    """The transcript type that applies to one file.

    The project's type is the rule; a file that names one of its own is the
    exception — a project holding a song next to an interview needs both, and
    only the file knows which of them it is. The answer carries the same
    `type_*` keys a project row does, so everything reading a type
    (`is_verbatim`, the cleanup, the export) works from it unchanged.
    """
    source = file_row if (file_row or {}).get("type_id") else project
    return {field: (source or {}).get(field) for field in TYPE_FIELDS}


def is_verbatim(project: dict[str, Any]) -> bool:
    """Whether a project's transcript type reproduces its material word for word.

    Read from a project row (`type_verbatim`, joined by `workspace`). On for
    every type that does not deliberately turn its material into something
    else, for a project without a type at all, and for a row that predates
    the field — reproducing the text is what the default promises. It decides
    one thing: whether the PDF export lets the LLM structure the text
    (`pdf.build_document`). How that text came about is `condenses()`.
    """
    value = project.get("type_verbatim")
    return True if value is None else bool(value)


def condenses(project: dict[str, Any]) -> bool:
    """Whether the type's aufbereitung writes a document of its own.

    Read from a project or file row (`type_condense`, joined by `workspace`).
    Off for a type that reproduces its material — which is the default, for a
    row without a type and for one that predates the field: the cleanup then
    runs section by section and every sentence comes back
    (`pipeline.cleanup_segments`). On for a type that turns the recording into
    something else, whose prompt runs once over the whole of it
    (`overview.reduce_document`).
    """
    return bool(project.get("type_condense"))


def diarizes(project: dict[str, Any]) -> bool:
    """Whether this type has the speakers recognised after the transcription.

    Read from a project or file row (`type_diarize`, joined by `workspace`).
    Off for a type that predates the field, for a row without a type and for
    every type that is one voice — recognising speakers on a song would split
    its verses between people who are not there. On, it costs the
    transcription its word timings (services/whisper.py) and one job per file
    (services/diarize.py).
    """
    return bool(project.get("type_diarize"))


def default_output_prompt() -> str:
    """The output-format prompt used when a type defines none of its own."""
    from .pdf import DEFAULT_OUTPUT_PROMPT  # local: pdf owns the block contract

    return DEFAULT_OUTPUT_PROMPT


def builtin_types() -> list[dict[str, str]]:
    """The builtins as they are stored: placeholders already expanded."""
    from .pdf import BLOCK_CONTRACT

    return [
        {
            **entry,
            # replace, not format: the contract itself is full of JSON braces
            "output_prompt": entry["output_prompt"].replace(CONTRACT_PLACEHOLDER, BLOCK_CONTRACT),
        }
        for entry in BUILTIN_TYPES
    ]


def seed_builtin_types() -> None:
    """Insert the builtin types exactly once per installation (at startup)."""
    entries = builtin_types()
    by_key = {entry["key"]: entry for entry in entries}
    with db.get_conn() as conn:
        if db.get_meta(conn, SEED_MARKER):
            _backfill_builtins(conn, entries)
            return
        for entry in entries:
            conn.execute(_INSERT_BUILTIN, _builtin_row(entry["key"], entry["name"], entry))
        for legacy_key, current_key in LEGACY_KEYS.items():
            entry = by_key[current_key]
            conn.execute(
                _INSERT_BUILTIN,
                _builtin_row(legacy_key, legacy_key.capitalize(), entry),
            )
        db.set_meta(conn, SEED_MARKER, "1")
        # a fresh install seeds every field right away, so the per-field
        # backfills below must not run over it at the next start
        for _field, marker, _untouched in _BACKFILL_FIELDS:
            db.set_meta(conn, marker, "1")


_BUILTIN_COLUMNS = (
    "key, name, structure, keep_sections, verbatim, condense, diarize, "
    "system_prompt, output_prompt, builtin"
)
_INSERT_BUILTIN = (
    f"INSERT OR IGNORE INTO project_types ({_BUILTIN_COLUMNS}) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)"
)


def _builtin_row(key: str, name: str, entry: dict[str, Any]) -> tuple[Any, ...]:
    # page breaks per section are off for every builtin: a compilation is
    # laid out the way it always was until somebody asks for it
    return (
        key,
        name,
        entry["structure"],
        int(bool(entry.get("keep_sections"))),
        int(bool(entry.get("verbatim", True))),
        int(bool(entry.get("condense"))),
        int(bool(entry.get("diarize"))),
        entry["system_prompt"],
        entry["output_prompt"],
    )


# (column, marker, the value that means "never set") — a field added after
# the builtins were seeded is filled in once, from the marker onwards.
_BACKFILL_FIELDS = (
    ("output_prompt", OUTPUT_PROMPT_MARKER, ""),
    ("structure", STRUCTURE_MARKER, "paragraphs"),
    ("verbatim", VERBATIM_MARKER, 1),
    ("condense", CONDENSE_MARKER, 0),
    ("diarize", DIARIZE_MARKER, 0),
)


def _backfill_builtins(conn, entries: list[dict[str, str]]) -> None:
    """Give already-seeded builtins the fields added after they were seeded.

    Each field carries its own marker and is only filled while it still holds
    the value it got from the schema default — anything the user has set stays.
    """
    by_key = {entry["key"]: entry for entry in entries}
    for legacy_key, current_key in LEGACY_KEYS.items():
        by_key[legacy_key] = by_key[current_key]
    for field, marker, untouched in _BACKFILL_FIELDS:
        if db.get_meta(conn, marker):
            continue
        # the column name comes from the literal tuple above, never from input
        sql = f"UPDATE project_types SET {field} = ? WHERE key = ? AND {field} = ?"
        for key, entry in by_key.items():
            conn.execute(sql, (entry[field], key, untouched))
        db.set_meta(conn, marker, "1")


def restore_builtin_types() -> list[dict[str, Any]]:
    """Re-insert deleted builtins and reset builtin settings to their defaults."""
    with db.get_conn() as conn:
        for entry in builtin_types():
            conn.execute(
                f"INSERT INTO project_types ({_BUILTIN_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET "
                "name = excluded.name, structure = excluded.structure, "
                "keep_sections = excluded.keep_sections, "
                "verbatim = excluded.verbatim, condense = excluded.condense, "
                "diarize = excluded.diarize, "
                "system_prompt = excluded.system_prompt, "
                "output_prompt = excluded.output_prompt, builtin = 1",
                _builtin_row(entry["key"], entry["name"], entry),
            )
    return list_types(include_legacy=False)


def list_types(*, include_legacy: bool = True) -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        sql = "SELECT * FROM project_types"
        params: tuple[str, ...] = ()
        if include_legacy:
            current_count = conn.execute(
                "SELECT COUNT(*) AS count FROM project_types WHERE key IN "
                f"({', '.join('?' for _ in BUILTIN_TYPES)})",
                tuple(entry["key"] for entry in BUILTIN_TYPES),
            ).fetchone()["count"]
            include_legacy = current_count == len(BUILTIN_TYPES)
        if not include_legacy:
            marks = ", ".join("?" for _ in LEGACY_KEYS)
            sql += f" WHERE key NOT IN ({marks})"
            params = tuple(LEGACY_KEYS)
        rows = conn.execute(sql + " ORDER BY builtin DESC, name", params).fetchall()
    return db.rows_to_dicts(rows)


def get_type(type_id: int) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM project_types WHERE id = ?", (type_id,)).fetchone()
    return db.row_to_dict(row)


def get_type_by_key(key: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM project_types WHERE key = ?", (key,)).fetchone()
    return db.row_to_dict(row)


def _unique_key(conn, base: str) -> str:
    key = base
    counter = 2
    while conn.execute("SELECT 1 FROM project_types WHERE key = ?", (key,)).fetchone():
        key = f"{base}-{counter}"
        counter += 1
    return key


def create_type(
    name: str,
    system_prompt: str,
    output_prompt: str = "",
    structure: str = "",
    keep_sections: bool = False,
    verbatim: bool = True,
    condense: bool = False,
    diarize: bool = False,
) -> dict[str, Any]:
    """Create a type; without an output prompt it starts from the default so
    the user has something to adapt rather than an empty field."""
    from .pdf import normalize_structure
    from .workspace import slugify

    with db.get_conn() as conn:
        key = _unique_key(conn, slugify(name))
        cursor = conn.execute(
            "INSERT INTO project_types "
            "(key, name, structure, keep_sections, verbatim, condense, diarize, "
            "system_prompt, output_prompt, builtin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                key,
                name,
                normalize_structure(structure),
                int(keep_sections),
                int(verbatim),
                int(condense),
                int(diarize),
                system_prompt,
                output_prompt or default_output_prompt(),
            ),
        )
        type_id = cursor.lastrowid
    return get_type(type_id)  # type: ignore[return-value]


def update_type(
    type_id: int,
    name: str,
    system_prompt: str,
    output_prompt: str = "",
    structure: str = "",
    keep_sections: bool = False,
    verbatim: bool = True,
    condense: bool = False,
    diarize: bool = False,
) -> dict[str, Any] | None:
    """Update a type. An emptied output prompt stays empty — the export then
    falls back to the default, which is a valid choice."""
    from .pdf import normalize_structure

    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE project_types SET name = ?, structure = ?, keep_sections = ?, "
            "verbatim = ?, condense = ?, diarize = ?, system_prompt = ?, "
            "output_prompt = ? WHERE id = ?",
            (
                name,
                normalize_structure(structure),
                int(keep_sections),
                int(verbatim),
                int(condense),
                int(diarize),
                system_prompt,
                output_prompt,
                type_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
    return get_type(type_id)


def delete_type(type_id: int) -> bool:
    """Delete a type (builtins included); whoever used it falls back.

    A project loses its type, a file falls back to whatever its project says —
    the same thing a file that never named a type of its own does.
    """
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET type_id = NULL WHERE type_id = ?", (type_id,))
        conn.execute("UPDATE files SET type_id = NULL WHERE type_id = ?", (type_id,))
        cursor = conn.execute("DELETE FROM project_types WHERE id = ?", (type_id,))
        return cursor.rowcount > 0
