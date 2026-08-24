"""Project types: named presets with an editable LLM system prompt.

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

BUILTIN_TYPES: list[dict[str, str]] = [
    {
        "key": "song",
        "name": "Song",
        "system_prompt": (
            "You are editing a song transcription. Preserve line breaks and recognizable "
            "repetitions (choruses). Structure the text into verses and choruses, correct "
            "obvious mishearings carefully, and do not invent any lines."
        ),
    },
    {
        "key": "interview",
        "name": "Interview/Dialogue",
        "system_prompt": (
            "You are editing an interview or dialogue transcription. Assign spoken "
            "contributions to speakers (Speaker 1, Speaker 2, ... or recognized names), "
            "remove filler words and false starts, but preserve the wording and meaning "
            "of the statements. Mark unclear passages with [inaudible]."
        ),
    },
    {
        "key": "speech",
        "name": "Speech",
        "system_prompt": (
            "You are editing a speech transcription. Divide the text into meaningful "
            "paragraphs, correct grammar and punctuation, and remove filler words. Keep "
            "quotations verbatim and mark them as quotations."
        ),
    },
    {
        "key": "protocol",
        "name": "Meeting Protocol",
        "system_prompt": (
            "You create meeting minutes from a conversation transcription. Summarize the "
            "discussion objectively, list decisions separately, and extract all tasks into "
            "a to-do list (who, what, by when, where stated). Strictly stay within the "
            "conversation's content."
        ),
    },
    {
        "key": "poem",
        "name": "Poem",
        "system_prompt": (
            "You are editing a poem transcription. Restore the stanza and verse structure, "
            "preserving rhythm, rhyme, and line breaks. Correct only unambiguous mishearings "
            "and do not change poetic wording."
        ),
    },
    {
        "key": "roleplay",
        "name": "Roleplay",
        "system_prompt": (
            "You are editing a roleplay or play transcription. Turn the text into a script: "
            "put character names before each spoken contribution and italicize stage "
            "directions in parentheses. Assign contributions to roles based on voice and "
            "context; mark unclear assignments with (?)."
        ),
    },
]

LEGACY_KEYS = {
    "lied": "song",
    "protokoll": "protocol",
    "gedicht": "poem",
    "rollenspiel": "roleplay",
}
BUILTIN_BY_KEY = {entry["key"]: entry for entry in BUILTIN_TYPES}


def seed_builtin_types() -> None:
    """Insert the builtin types exactly once per installation (at startup)."""
    with db.get_conn() as conn:
        for legacy_key, current_key in LEGACY_KEYS.items():
            entry = BUILTIN_BY_KEY[current_key]
            conn.execute(
                "UPDATE project_types SET key = ?, name = ?, system_prompt = ? "
                "WHERE key = ? AND builtin = 1",
                (current_key, entry["name"], entry["system_prompt"], legacy_key),
            )
        if db.get_meta(conn, SEED_MARKER):
            return
        for entry in BUILTIN_TYPES:
            conn.execute(
                "INSERT OR IGNORE INTO project_types (key, name, system_prompt, builtin) "
                "VALUES (?, ?, ?, 1)",
                (entry["key"], entry["name"], entry["system_prompt"]),
            )
        db.set_meta(conn, SEED_MARKER, "1")


def restore_builtin_types() -> list[dict[str, Any]]:
    """Re-insert deleted builtins and reset builtin prompts to their defaults."""
    with db.get_conn() as conn:
        for entry in BUILTIN_TYPES:
            conn.execute(
                "INSERT INTO project_types (key, name, system_prompt, builtin) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET "
                "name = excluded.name, system_prompt = excluded.system_prompt, builtin = 1",
                (entry["key"], entry["name"], entry["system_prompt"]),
            )
    return list_types()


def list_types() -> list[dict[str, Any]]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM project_types ORDER BY builtin DESC, name").fetchall()
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


def create_type(name: str, system_prompt: str) -> dict[str, Any]:
    from .workspace import slugify

    with db.get_conn() as conn:
        key = _unique_key(conn, slugify(name))
        cursor = conn.execute(
            "INSERT INTO project_types (key, name, system_prompt, builtin) VALUES (?, ?, ?, 0)",
            (key, name, system_prompt),
        )
        type_id = cursor.lastrowid
    return get_type(type_id)  # type: ignore[return-value]


def update_type(type_id: int, name: str, system_prompt: str) -> dict[str, Any] | None:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "UPDATE project_types SET name = ?, system_prompt = ? WHERE id = ?",
            (name, system_prompt, type_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_type(type_id)


def delete_type(type_id: int) -> bool:
    """Delete a type (builtins included); projects using it fall back to no type."""
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET type_id = NULL WHERE type_id = ?", (type_id,))
        cursor = conn.execute("DELETE FROM project_types WHERE id = ?", (type_id,))
        return cursor.rowcount > 0
