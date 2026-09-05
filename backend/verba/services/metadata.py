"""Metadata extraction from audio tags (via `av`) and filename schemes.

Filenames may use ``date_source-target_title_addition.ext``. Empty fields
are allowed; MP3/MP4 tags may still provide a title or date. A tag title wins
over the one from the file name, a tag date only fills in what the name does
not state — see `extract_metadata`.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from .languages import LANGUAGE_NAMES

logger = logging.getLogger(__name__)

_FILENAME_DATE = re.compile(
    r"^(?P<year>\d{4})[-_.]?(?P<month>\d{2})[-_.]?(?P<day>\d{2})[-_ .]*(?P<rest>.*)$"
)


_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def format_display_date(value: str) -> str:
    """Format ISO dates for display in a PDF header.

    The header's right-hand field is free text, so it may carry a place as
    well ("München, 2024-08-17"). Every ISO date inside the value is
    formatted; everything else is passed through untouched — a date the user
    already wrote as "28.01.1933" therefore stays as it is.
    """

    def replace(match: re.Match[str]) -> str:
        try:
            return date.fromisoformat(match.group(0)).strftime("%d.%m.%Y")
        except ValueError:
            return match.group(0)  # e.g. 2024-13-45

    return _ISO_DATE.sub(replace, value)


def _field_date(field: str) -> str:
    """The ISO date a whole field states, or "" — 20240817, 2024-08-17, 2024.08.17."""
    match = re.fullmatch(r"(\d{4})[-.]?(\d{2})[-.]?(\d{2})", field)
    if not match:
        return ""
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return ""  # 20241301 and friends


def _is_language_slot(field: str) -> bool:
    """A scheme slot for a language: a code Whisper knows, or left empty."""
    return field == "" or field.lower() in LANGUAGE_NAMES


def _parse_filename(stem: str) -> dict[str, str]:
    result = {"title": "", "recorded_at": "", "language": "", "target_language": "", "addition": ""}

    # The scheme is read by position, not by counting fields: a date, then up to
    # two language slots, then what is left. Requiring all five fields made
    # "20260731_ru_de_Wesner Ronald" fall through to the plain path, where the
    # language codes ended up inside the title. Only a field that is a language
    # code (or empty for "not given") is taken as one, so a title is never
    # swallowed, and nothing past the fifth field is dropped any more.
    fields = stem.split("_")
    scheme_date = _field_date(fields[0])
    if scheme_date:
        result["recorded_at"] = scheme_date
        rest = fields[1:]
        for key in ("language", "target_language"):
            if not rest or not _is_language_slot(rest[0]):
                break
            result[key] = rest.pop(0).lower()
        if rest:
            # the last field is the addition — but only where the name says
            # more than the title, so a title with underscores stays whole
            result["title"] = " ".join(rest[:-1]) if len(rest) > 1 else rest[0]
            result["addition"] = rest[-1] if len(rest) > 1 else ""
        return result

    match = _FILENAME_DATE.match(stem)
    rest = stem
    if match:
        try:
            parsed = date(int(match["year"]), int(match["month"]), int(match["day"]))
            result["recorded_at"] = parsed.isoformat()
            rest = match["rest"]
        except ValueError:
            pass  # e.g. "12345678_" that is not a real date
    title = re.sub(r"[_]+", " ", rest).strip(" -_")
    result["title"] = title
    return result


def _read_tags(path: Path) -> dict[str, str]:
    try:
        import av
    except ImportError:
        return {}
    try:
        with av.open(str(path)) as container:
            return {key.lower(): value for key, value in (container.metadata or {}).items()}
    except Exception as exc:
        logger.warning("could not read tags of %s: %s", path, exc)
        return {}


def _normalize_tag_date(raw: str) -> str:
    match = re.match(r"(\d{4})(?:[-/.]?(\d{2})(?:[-/.]?(\d{2}))?)?", raw.strip())
    if not match:
        return ""
    year, month, day = match.group(1), match.group(2) or "01", match.group(3) or "01"
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""


def extract_metadata(path: Path) -> dict[str, Any]:
    """Return metadata parsed from tags and the optional filename scheme."""
    result = _parse_filename(path.stem)
    tags = _read_tags(path)

    if tags.get("title"):
        result["title"] = tags["title"].strip()
    # A date in the file name was typed for exactly this purpose, so it stands:
    # tags only supply the date the name does not give. That matters most for
    # `creation_time`, which is not authored at all — it is whatever muxed the
    # file last, and for a recording copied off a device or re-encoded that is
    # the day it was processed, not the day it was recorded. Among the tags the
    # authored ones therefore come first.
    if not result["recorded_at"]:
        for key in ("date", "year", "creation_time"):
            if tags.get(key):
                normalized = _normalize_tag_date(tags[key])
                if normalized:
                    result["recorded_at"] = normalized
                    break
    return result
