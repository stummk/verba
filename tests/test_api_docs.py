from __future__ import annotations

from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs" / "user"


def test_all_language_guides_exist():
    for lang in ("de", "en", "ru"):
        assert (DOCS_DIR / f"{lang}.md").exists(), f"docs/user/{lang}.md fehlt"


def test_docs_endpoint_returns_requested_language(client):
    for lang in ("de", "en", "ru"):
        data = client.get("/api/docs", params={"lang": lang}).json()
        assert data["lang"] == lang
        assert data["content"].startswith("# Verba")


def test_docs_endpoint_falls_back_to_german(client):
    data = client.get("/api/docs", params={"lang": "xx"}).json()
    assert data["lang"] == "de"
    assert "Benutzerhandbuch" in data["content"]


def test_docs_endpoint_default_language(client):
    assert client.get("/api/docs").json()["lang"] == "de"
