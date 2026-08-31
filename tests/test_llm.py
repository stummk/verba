from __future__ import annotations

import httpx
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


# ── answers of reasoning models ────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<think>erst denken</think>Das Ergebnis.", "Das Ergebnis."),
        ("<think>a</think>Eins<think>b</think>Zwei", "EinsZwei"),
        ("<THINK>laut</THINK>\nErgebnis", "Ergebnis"),
        ("<thinking>x</thinking> Ergebnis ", "Ergebnis"),
        ("kein Tag am Anfang</think>Ergebnis", "Ergebnis"),  # server dropped the opener
        ("<think>abgeschnitten mitten im Denken", ""),  # nothing usable followed
        ("Ganz normale Antwort.", "Ganz normale Antwort."),
    ],
)
def test_strip_reasoning(raw, expected):
    assert llm.strip_reasoning(raw) == expected


def completion(content, finish_reason="stop", **extra):
    message = {"role": "assistant", "content": content, **extra}
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def configure_endpoint(monkeypatch):
    settings = configured("openai", "http://localhost:1234/v1")
    settings.llm.model = "qwen"
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)


def fake_post(payload, status_code=200):
    def post(url, json=None, headers=None, timeout=None):
        return httpx.Response(status_code, json=payload, request=httpx.Request("POST", url))

    return post


def test_chat_returns_the_answer_without_the_thinking(monkeypatch):
    configure_endpoint(monkeypatch)
    monkeypatch.setattr(
        llm.httpx, "post", fake_post(completion("<think>hm</think>Bereinigter Text."))
    )
    assert llm.chat([{"role": "user", "content": "x"}]) == "Bereinigter Text."


def test_chat_joins_content_parts(monkeypatch):
    configure_endpoint(monkeypatch)
    monkeypatch.setattr(
        llm.httpx, "post", fake_post(completion([{"text": "Teil 1 "}, {"text": "Teil 2"}]))
    )
    assert llm.chat([{"role": "user", "content": "x"}]) == "Teil 1 Teil 2"


def test_reasoning_only_answer_raises_instead_of_returning_nothing(monkeypatch):
    """LM Studio with a reasoning model: content empty, budget spent thinking."""
    configure_endpoint(monkeypatch)
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        return httpx.Response(
            200,
            json=completion("", finish_reason="length", reasoning_content="denk denk"),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(llm.LLMError) as excinfo:
        llm.chat([{"role": "user", "content": "x"}])
    assert len(calls) == llm.MAX_ATTEMPTS  # an empty answer is worth a retry
    assert "Token-Budget" in str(excinfo.value)
    assert "Reasoning" in str(excinfo.value)


def test_error_message_carries_the_endpoint_explanation(monkeypatch):
    configure_endpoint(monkeypatch)
    monkeypatch.setattr(
        llm.httpx,
        "post",
        fake_post({"error": {"message": "model not loaded"}}, status_code=400),
    )
    with pytest.raises(llm.LLMError, match="model not loaded"):
        llm.chat([{"role": "user", "content": "x"}])


def test_no_token_cap_is_sent_by_default(monkeypatch):
    """A cap would shorten the cleaned transcript — the context is the limit."""
    configure_endpoint(monkeypatch)
    bodies = []

    def post(url, json=None, headers=None, timeout=None):
        bodies.append(json)
        return httpx.Response(200, json=completion("Text"), request=httpx.Request("POST", url))

    monkeypatch.setattr(llm.httpx, "post", post)
    llm.chat([{"role": "user", "content": "x"}])
    assert "max_tokens" not in bodies[0]

    llm.chat([{"role": "user", "content": "x"}], max_tokens=512)  # a short answer on request
    assert bodies[1]["max_tokens"] == 512


def test_a_cut_off_answer_is_not_returned_as_a_result(monkeypatch):
    configure_endpoint(monkeypatch)
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        return httpx.Response(
            200,
            json=completion("Halber Sat", finish_reason="length"),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(llm.httpx, "post", post)
    with pytest.raises(llm.TruncatedAnswer) as excinfo:
        llm.chat([{"role": "user", "content": "x"}])
    assert len(calls) == 1  # no retry: the same request would be cut off again
    assert excinfo.value.text == "Halber Sat"
    assert "brach ab" in str(excinfo.value)


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
