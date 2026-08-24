"""The three UI language catalogs must stay in sync (same keys, no empties)."""

from __future__ import annotations

import json
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parents[1] / "frontend" / "i18n"
LANGUAGES = ["de", "en", "ru"]


def load(lang: str) -> dict:
    return json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_all_catalogs_exist():
    for lang in LANGUAGES:
        assert (I18N_DIR / f"{lang}.json").exists(), f"{lang}.json fehlt"


def test_catalogs_have_identical_keys():
    reference = set(load("de").keys())
    for lang in ("en", "ru"):
        keys = set(load(lang).keys())
        assert keys == reference, (
            f"{lang}.json weicht ab — fehlend: {sorted(reference - keys)}, "
            f"überzählig: {sorted(keys - reference)}"
        )


def test_no_empty_translations():
    for lang in LANGUAGES:
        for key, value in load(lang).items():
            assert isinstance(value, str) and value.strip(), f"{lang}.json: '{key}' ist leer"


def test_placeholders_match_german_reference():
    import re

    def placeholders(text: str) -> set[str]:
        return set(re.findall(r"\{(\w+)\}", text))

    reference = load("de")
    for lang in ("en", "ru"):
        catalog = load(lang)
        for key, value in reference.items():
            assert placeholders(catalog[key]) == placeholders(value), (
                f"{lang}.json: Platzhalter in '{key}' stimmen nicht mit de.json überein"
            )
