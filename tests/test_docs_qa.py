"""Questions about the user guide: prefiltering, context limits, availability.

The guide is bigger than a small context window, so the interesting part is
what ends up in the prompt — and that a context-length error leads to a
smaller prompt instead of a failed request.
"""

from __future__ import annotations

import pytest

from verba import config
from verba.services import docs_qa, llm

GUIDE = """# Verba

Intro paragraph about the app.

## Installation {#install}

Download the installer and double-click it.

## PDF export {#pdf}

Translations can go into one PDF, separated by a divider line.
The header stays in the original language.

## Search {#search}

The index is built automatically while transcribing.
"""


def long_guide(count: int = 6, size: int = 2500) -> str:
    """A guide whose sections do not all fit into a small budget."""
    paragraph = "Export Absatz." + " Text." * 20
    body = "\n\n".join([paragraph] * max(size // len(paragraph), 2))
    parts = ["# Verba", "", "Intro.", ""]
    for index in range(1, count + 1):
        parts.extend([f"## Abschnitt {index} {{#s{index}}}", "", body, ""])
    return "\n".join(parts)


def configure_llm(monkeypatch, mode="openai"):
    settings = config.get_settings()
    settings.llm.mode = mode
    settings.llm.base_url = "http://localhost:1234/v1"
    settings.llm.model = "test-model"
    config.save_settings(settings)


@pytest.fixture()
def guide(tmp_path, monkeypatch):
    path = tmp_path / "de.md"
    path.write_text(GUIDE, encoding="utf-8")
    monkeypatch.setattr(docs_qa, "guide_path", lambda lang=docs_qa.DEFAULT_LANG: path)
    return path


# ── splitting the guide ───────────────────────────────────────────────


def test_sections_carry_the_slug_from_the_heading():
    intro, sections = docs_qa.split_sections(GUIDE)
    assert intro.startswith("# Verba")
    assert [s.slug for s in sections] == ["install", "pdf", "search"]
    assert [s.title for s in sections] == ["Installation", "PDF export", "Search"]
    assert "divider line" in sections[1].body


def test_the_shipped_guides_all_carry_slugs_the_ui_can_map():
    """The icon map in frontend/js/views/docs.js is keyed by these slugs."""
    slugs = None
    for lang in docs_qa.SUPPORTED:
        text = (docs_qa.docs_dir() / f"{lang}.md").read_text(encoding="utf-8")
        _, sections = docs_qa.split_sections(text)
        assert sections, f"{lang}: no sections"
        assert all(s.slug for s in sections), f"{lang}: section without a slug"
        if slugs is None:
            slugs = [s.slug for s in sections]
        else:  # same sections in the same order in every language
            assert [s.slug for s in sections] == slugs


def test_a_heading_inside_a_code_fence_is_not_a_section():
    markdown = "Intro\n\n```\n## not a heading {#nope}\n```\n\n## Real {#real}\n\nBody\n"
    _, sections = docs_qa.split_sections(markdown)
    assert [s.slug for s in sections] == ["real"]


# ── picking the context ───────────────────────────────────────────────


def test_the_matching_section_wins():
    _, sections = docs_qa.split_sections(GUIDE)
    chosen, _ = docs_qa.select_context("Wo finde ich den Index der Suche?", sections, budget=100)
    assert [s.slug for s in chosen] == ["search"]


def test_sections_stay_in_document_order():
    _, sections = docs_qa.split_sections(GUIDE)
    chosen, truncated = docs_qa.select_context("PDF Installation Search", sections, budget=10000)
    assert [s.slug for s in chosen] == ["install", "pdf", "search"]
    assert truncated is False


def test_a_question_without_matches_still_gets_context():
    _, sections = docs_qa.split_sections(GUIDE)
    chosen, _ = docs_qa.select_context("zzzz", sections, budget=10000)
    assert chosen, "an unmatched question must not produce an empty prompt"


def test_a_section_larger_than_the_budget_is_shortened_not_dropped():
    _, sections = docs_qa.split_sections(long_guide())
    chosen, truncated = docs_qa.select_context("Export", sections, budget=900)
    assert [s.slug for s in chosen] == ["s1"]
    assert truncated is True
    assert chosen[0].body.endswith("[…]")
    assert len(chosen[0].text) < 1200


def test_short_words_like_pdf_still_rank():
    """Three characters is the minimum, because PDF, LLM and API matter here."""
    _, sections = docs_qa.split_sections(GUIDE)
    chosen, _ = docs_qa.select_context("Wie exportiere ich eine PDF?", sections, budget=120)
    assert [s.slug for s in chosen] == ["pdf"]


# ── asking ────────────────────────────────────────────────────────────


def test_ask_sends_only_the_guide_and_the_question(guide, monkeypatch):
    seen: list = []
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: seen.append(messages) or "Antwort.")

    result = docs_qa.ask("Wie exportiere ich eine PDF?")

    assert result["answer"] == "Antwort."
    assert [s["slug"] for s in result["sections"]]
    [messages] = seen
    assert messages[0]["role"] == "system"
    assert "user guide" in messages[0]["content"]
    assert "divider line" in messages[1]["content"]  # the pdf section came along
    assert messages[1]["content"].endswith("Wie exportiere ich eine PDF?")


def test_a_context_error_retries_with_a_smaller_prompt(tmp_path, monkeypatch):
    path = tmp_path / "de.md"
    path.write_text(long_guide(), encoding="utf-8")
    monkeypatch.setattr(docs_qa, "guide_path", lambda lang=docs_qa.DEFAULT_LANG: path)
    lengths: list[int] = []

    def chat(messages, **kw):
        lengths.append(len(messages[1]["content"]))
        if len(lengths) < 3:
            raise llm.LLMError("HTTP 400: maximum context length exceeded")
        return "Kurze Antwort."

    monkeypatch.setattr(llm, "chat", chat)
    result = docs_qa.ask("Wie exportiere ich einen Export?")

    assert result["answer"] == "Kurze Antwort."
    assert len(lengths) == 3
    assert lengths[0] > lengths[-1], "the retry must shrink the prompt"


def test_an_unrelated_llm_error_is_not_retried(guide, monkeypatch):
    calls = []

    def chat(messages, **kw):
        calls.append(1)
        raise llm.LLMError("connection refused")

    monkeypatch.setattr(llm, "chat", chat)
    with pytest.raises(llm.LLMError, match="connection refused"):
        docs_qa.ask("Frage?")
    assert len(calls) == 1


def test_a_context_error_that_never_clears_fails_with_a_message(guide, monkeypatch):
    monkeypatch.setattr(
        llm,
        "chat",
        lambda messages, **kw: (_ for _ in ()).throw(llm.LLMError("context window exceeded")),
    )
    with pytest.raises(llm.LLMError, match="zu wenig Kontext"):
        docs_qa.ask("Frage?")


# ── availability & API ────────────────────────────────────────────────


def test_availability_follows_the_llm_configuration(monkeypatch):
    assert docs_qa.available() is False  # default: mode "none"
    configure_llm(monkeypatch)
    assert docs_qa.available() is True


def test_get_docs_reports_whether_questions_are_possible(client, monkeypatch):
    assert client.get("/api/docs").json()["llm_available"] is False
    configure_llm(monkeypatch)
    assert client.get("/api/docs").json()["llm_available"] is True


def test_ask_endpoint_without_an_llm_is_refused(client):
    response = client.post("/api/docs/ask", json={"question": "Wie exportiere ich?"})
    assert response.status_code == 409
    assert "Sprachmodell" in response.json()["detail"]


def test_ask_endpoint_returns_the_answer(client, monkeypatch):
    configure_llm(monkeypatch)
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "Über Einstellungen → Export.")

    response = client.post(
        "/api/docs/ask", json={"question": "Wie exportiere ich eine PDF?", "lang": "de"}
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "Über Einstellungen → Export."


def test_a_dead_endpoint_becomes_a_bad_gateway(client, monkeypatch):
    configure_llm(monkeypatch)
    monkeypatch.setattr(
        llm, "chat", lambda messages, **kw: (_ for _ in ()).throw(llm.LLMError("no route to host"))
    )
    response = client.post("/api/docs/ask", json={"question": "Frage?"})
    assert response.status_code == 502


def test_a_too_short_question_is_rejected(client, monkeypatch):
    configure_llm(monkeypatch)
    assert client.post("/api/docs/ask", json={"question": "?"}).status_code == 422


def test_at_most_six_sections_reach_the_prompt():
    """The whole guide fits the budget, so the section cap keeps it focused."""
    _, sections = docs_qa.split_sections(long_guide(count=12, size=300))
    chosen, truncated = docs_qa.select_context("Export", sections, budget=100000)
    assert len(chosen) == docs_qa.MAX_SECTIONS
    assert truncated is True


def test_unrelated_sections_stay_out_when_something_matches():
    _, sections = docs_qa.split_sections(GUIDE)
    chosen, _ = docs_qa.select_context("Wie exportiere ich eine PDF?", sections, budget=100000)
    assert [s.slug for s in chosen] == ["pdf"]
