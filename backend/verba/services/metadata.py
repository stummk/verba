"""Metadata extraction from audio tags (via `av`) and filename schemes.

Filenames may use ``date_source-target_title_addition.ext``. Empty fields
are allowed; MP3/MP4 tags may still provide a title or date.
Tag values win over filename-derived values.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FILENAME_DATE = re.compile(
    r"^(?P<year>\d{4})[-_.]?(?P<month>\d{2})[-_.]?(?P<day>\d{2})[-_ .]*(?P<rest>.*)$"
)


def format_display_date(value: str) -> str:
    """Format an ISO date for display in a PDF header."""
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value


def _parse_filename(stem: str) -> dict[str, str]:
    result = {"title": "", "recorded_at": "", "language": "", "target_language": "", "addition": ""}
    fields = stem.split("_")
    if len(fields) >= 5 and fields[0].isdigit():
        (
            raw_date,
            result["language"],
            result["target_language"],
            result["title"],
            result["addition"],
        ) = fields[:5]
        try:
            parsed = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
            if len(raw_date) == 8:
                result["recorded_at"] = parsed.isoformat()
        except (ValueError, IndexError):
            pass
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
    for key in ("date", "creation_time", "year"):
        if tags.get(key):
            normalized = _normalize_tag_date(tags[key])
            if normalized:
                result["recorded_at"] = normalized
                break
    return result
