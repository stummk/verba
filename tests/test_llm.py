from __future__ import annotations

import pytest

from verba import config
from verba.services import llm, metadata


def configured(mode: str, base_url: str = "") -> config.Settings:
    settings = config.Settings()
    settings.llm.mode = mode  # type: ignore[assignment]
    settings.llm.base_url = base_url
    return settings


def test_location_none():
    assert llm.llm_location(configured("none")) == "none"


def test_location_local_mode():
    assert llm.llm_location(configured("local")) == "local"


def test_location_remote_endpoint():
    assert llm.llm_location(configured("openai", "https://api.openai.com/v1")) == "remote"


def test_location_localhost_endpoint_counts_as_local():
    assert llm.llm_location(configured("openai", "http://localhost:8080/v1")) == "local"
    assert llm.llm_location(configured("openai", "http://127.0.0.1:11434/v1")) == "local"


def test_chat_without_configuration_raises():
    config.reset_cache()
    with pytest.raises(llm.LLMNotConfigured):
        llm.chat([{"role": "user", "content": "hi"}])


# ── metadata extraction ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stem", "title", "recorded_at"),
    [
        ("20240817_Rede vom Sonntag", "Rede vom Sonntag", "2024-08-17"),
        ("2024-08-17 Interview", "Interview", "2024-08-17"),
        ("20240817-Lied_Nummer_3", "Lied Nummer 3", "2024-08-17"),
        ("Nur_ein_Titel", "Nur ein Titel", ""),
        ("99999999_kein_datum", "99999999 kein datum", ""),
        ("20241301_ungueltig", "20241301 ungueltig", ""),
    ],
)
def test_filename_parsing(stem, title, recorded_at, tmp_path):
    path = tmp_path / f"{stem}.mp3"
    path.write_bytes(b"not-audio")  # tags unreadable -> filename wins
    result = metadata.extract_metadata(path)
    assert result["title"] == title
    assert result["recorded_at"] == recorded_at


def test_filename_scheme_parses_languages_and_header_fields(tmp_path):
    path = tmp_path / "20260401_de__Titel_Zusatz.mp3"
    path.write_bytes(b"not-audio")
    result = metadata.extract_metadata(path)
    assert result["recorded_at"] == "2026-04-01"
    assert result["language"] == "de"
    assert result["target_language"] == ""
    assert result["title"] == "Titel"
    assert result["addition"] == "Zusatz"
    assert metadata.format_display_date(result["recorded_at"]) == "01.04.2026"
