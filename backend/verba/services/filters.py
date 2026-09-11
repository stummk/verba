"""The file filters the transcript overview and the search share.

One set of choices per field, every field AND-ed with the next, and a field
the user left alone is no filter at all. They are about *files* — a transcript
matches as soon as one of its files does — so the same clause narrows the
overview and the hit list, and a filter set means the same thing in both.

`options()` answers what there is to choose from: only values that actually
occur in the material this user may see, so a picker never offers a language
nobody recorded in.
"""

from __future__ import annotations

from typing import Any

from .. import db

# A file without its own date falls back to the day it was imported: every
# file has one then, so a date range never silently drops the untagged half.
FILE_DATE = "COALESCE(NULLIF(f.recorded_at, ''), substr(f.created_at, 1, 10))"

# The type that applies to the file, not the one the project prescribes.
FILE_TYPE = "COALESCE(f.type_id, p.type_id)"

# Whether a speaker is heard in a file. The chunk index asks the same question
# of its own `speakers` column (see vectorstore) — hence a per-value snippet
# rather than one fixed clause.
FILE_SPEAKER_SQL = "EXISTS (SELECT 1 FROM segments sp WHERE sp.file_id = f.id AND sp.speaker = ?)"

# How many speakers a picker is offered at most. A house with hundreds of
# transcripts has hundreds of names, and a dialog is not a directory.
MAX_SPEAKER_OPTIONS = 300

LIST_FIELDS = ("type_ids", "languages", "speakers", "statuses")
FIELDS = (*LIST_FIELDS, "date_from", "date_to")


def normalise(raw: dict[str, Any] | None) -> dict[str, Any]:
    """The filter set as the clause builder wants it: lists and two dates.

    Everything unknown is dropped and everything empty is left out, so
    `is_empty()` can say "no filter" by looking at the result alone.
    """
    raw = raw or {}
    result: dict[str, Any] = {}
    for field in LIST_FIELDS:
        values = raw.get(field) or []
        if isinstance(values, str):
            values = [values]
        cleaned = []
        for value in values:
            if field == "type_ids":
                try:
                    cleaned.append(int(value))
                except (TypeError, ValueError):
                    continue
            elif str(value).strip():
                cleaned.append(str(value).strip())
        if cleaned:
            result[field] = cleaned
    for field in ("date_from", "date_to"):
        value = str(raw.get(field) or "").strip()
        if value:
            result[field] = value
    return result


def is_empty(filters: dict[str, Any] | None) -> bool:
    return not any(filters.get(field) for field in FIELDS) if filters else True


def clause(
    filters: dict[str, Any] | None,
    speaker_sql: str = FILE_SPEAKER_SQL,
    speaker_value=lambda name: name,
) -> tuple[str, list[Any]]:
    """SQL over a `files f` joined to `projects p`, ready to append to a WHERE.

    Several values in one field are an OR — picking German and English means
    "either" — while two different fields are an AND.
    """
    filters = filters or {}
    sql = ""
    params: list[Any] = []

    type_ids = filters.get("type_ids")
    if type_ids:
        marks = ", ".join("?" for _ in type_ids)
        sql += f" AND {FILE_TYPE} IN ({marks})"  # noqa: S608 — placeholders only
        params.extend(type_ids)
    languages = filters.get("languages")
    if languages:
        marks = ", ".join("?" for _ in languages)
        sql += f" AND f.language IN ({marks})"  # noqa: S608 — placeholders only
        params.extend(languages)
    statuses = filters.get("statuses")
    if statuses:
        marks = ", ".join("?" for _ in statuses)
        sql += f" AND f.status IN ({marks})"  # noqa: S608 — placeholders only
        params.extend(statuses)
    speakers = filters.get("speakers")
    if speakers:
        sql += " AND (" + " OR ".join(speaker_sql for _ in speakers) + ")"
        params.extend(speaker_value(name) for name in speakers)
    if filters.get("date_from"):
        sql += f" AND {FILE_DATE} >= ?"
        params.append(filters["date_from"])
    if filters.get("date_to"):
        sql += f" AND {FILE_DATE} <= ?"
        params.append(filters["date_to"])
    return sql, params


def options(user: dict[str, Any] | None = None) -> dict[str, Any]:
    """What there is to filter by, counted over the files this user may see."""
    from .auth import visibility_clause  # local import: auth imports the db layer

    visible, visible_params = visibility_clause(user)
    where = f" WHERE {visible}" if visible else ""
    base = "FROM files f JOIN projects p ON p.id = f.project_id"

    with db.get_conn() as conn:
        types = conn.execute(
            f"SELECT {FILE_TYPE} AS type_id, t.name AS name, COUNT(*) AS count "  # noqa: S608
            f"{base} LEFT JOIN project_types t ON t.id = {FILE_TYPE}{where} "
            "GROUP BY {type} HAVING {type} IS NOT NULL ORDER BY t.name".format(type=FILE_TYPE),
            visible_params,
        ).fetchall()
        languages = conn.execute(
            f"SELECT f.language AS code, COUNT(*) AS count {base}{where} "  # noqa: S608
            "GROUP BY f.language HAVING f.language != '' ORDER BY count DESC, code",
            visible_params,
        ).fetchall()
        statuses = conn.execute(
            f"SELECT f.status AS status, COUNT(*) AS count {base}{where} "  # noqa: S608
            "GROUP BY f.status ORDER BY count DESC, status",
            visible_params,
        ).fetchall()
        speakers = conn.execute(
            "SELECT sp.speaker AS name, COUNT(DISTINCT f.id) AS count "  # noqa: S608
            f"{base} JOIN segments sp ON sp.file_id = f.id{where} "
            "GROUP BY sp.speaker HAVING sp.speaker != '' "
            "ORDER BY count DESC, name LIMIT ?",
            [*visible_params, MAX_SPEAKER_OPTIONS],
        ).fetchall()
        dates = conn.execute(
            f"SELECT MIN({FILE_DATE}) AS first, MAX({FILE_DATE}) AS last "  # noqa: S608
            f"{base}{where}",
            visible_params,
        ).fetchone()

    return {
        "types": [{"id": row["type_id"], "name": row["name"] or ""} for row in types],
        "languages": [{"code": row["code"], "count": row["count"]} for row in languages],
        "statuses": [{"status": row["status"], "count": row["count"]} for row in statuses],
        "speakers": [{"name": row["name"], "count": row["count"]} for row in speakers],
        "date_first": (dates["first"] if dates else "") or "",
        "date_last": (dates["last"] if dates else "") or "",
    }
