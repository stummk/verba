"""The language chip: its flag table has to stay in step with the language
list, and the three places that show a language have to use the chip — that is
the whole point of having one component for it."""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
LANGUAGES = FRONTEND / "js/languages.js"


def whisper_codes() -> set[str]:
    source = LANGUAGES.read_text(encoding="utf-8")
    match = re.search(r"const WHISPER_LANGUAGES = \[(.*?)\];", source, re.DOTALL)
    assert match, "WHISPER_LANGUAGES not found"
    return set(re.findall(r'"([a-z-]+)"', match.group(1)))


def regions() -> dict[str, str]:
    source = LANGUAGES.read_text(encoding="utf-8")
    match = re.search(r"const REGIONS = \{(.*?)\n\};", source, re.DOTALL)
    assert match, "REGIONS not found"
    return dict(re.findall(r"([a-z]{2,3}): \"([A-Z]{2})\"", match.group(1)))


def test_every_flag_belongs_to_a_language_whisper_knows():
    unknown = set(regions()) - whisper_codes()
    assert not unknown, f"REGIONS nennt unbekannte Sprachen: {sorted(unknown)}"


def test_every_region_is_a_two_letter_code():
    """The flag emoji is built from the two letters — anything else would
    produce a stray glyph instead of a flag."""
    source = LANGUAGES.read_text(encoding="utf-8")
    match = re.search(r"const REGIONS = \{(.*?)\n\};", source, re.DOTALL)
    entries = re.findall(r"([a-z]+):\s*\"([^\"]*)\"", match.group(1))
    assert entries, "REGIONS parsed empty — regex broken?"
    for code, region in entries:
        assert re.fullmatch(r"[A-Z]{2}", region), f"{code}: {region!r} ist kein Ländercode"


def test_languages_without_one_country_carry_no_flag():
    """A flag is a country's, not a language's. Regional and stateless
    languages get the globe instead — choosing a state for them would be a
    claim, not a label."""
    mapped = set(regions())
    for code in ("ca", "eu", "gl", "bo", "yue", "la", "br", "oc", "cy", "yi"):
        assert code not in mapped, f"{code} sollte keine Fahne haben"


def test_the_three_language_spots_use_the_chip():
    project = (FRONTEND / "js/views/project.js").read_text(encoding="utf-8")
    editor = (FRONTEND / "js/views/editor.js").read_text(encoding="utf-8")
    for name, source in (("project.js", project), ("editor.js", editor)):
        assert 'from "../language-chip.js"' in source, f"{name} nutzt den Chip nicht"
    # the file list, the recording under the waveform, the translations
    assert project.count("languageChip({") == 1
    assert editor.count("languageChip({") == 2
    # ... and no <select> is left behind in any of them
    assert "file-language" not in project
    assert 'class="lang-select"' not in editor


def test_the_picker_texts_exist_in_every_catalog():
    import json

    keys = {
        "picker.language",
        "picker.filter",
        "picker.noMatch",
        "picker.auto",
        "picker.autoTitle",
    }
    for lang in ("de", "en", "ru"):
        catalog = json.loads((FRONTEND / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert keys <= set(catalog), f"{lang}.json fehlt: {sorted(keys - set(catalog))}"
