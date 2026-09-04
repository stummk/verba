"""Public OpenAI-compatible API: key management, auth, formats, cleanup."""

from __future__ import annotations

import io

import pytest

from verba.core.jobs import lane_for_kind
from verba.services import public_api, whisper

SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": "Hallo Welt."},
    {"start": 2.5, "end": 61.2, "text": "Zweiter Satz."},
]


@pytest.fixture(autouse=True)
def fake_whisper(monkeypatch):
    def fake_transcribe_path(audio_path, *, language="", model_override="", cancel, report):
        report(50, "halbzeit")
        return {"segments": list(SEGMENTS), "language": language or "de", "duration": 61.2}

    monkeypatch.setattr(whisper, "transcribe_path", fake_transcribe_path)


def post_audio(client, data=None, headers=None):
    return client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", io.BytesIO(b"RIFFfake"), "audio/wav")},
        data=data or {},
        headers=headers or {},
    )


# ── key management ────────────────────────────────────────────────────


def test_create_key_returns_plaintext_exactly_once(client):
    created = client.post("/api/apikeys", json={"name": "CI"}).json()
    assert created["key"].startswith("vb-")
    assert created["prefix"] == created["key"][:11]

    listed = client.get("/api/apikeys").json()
    assert len(listed) == 1
    assert "key" not in listed[0] and "key_hash" not in listed[0]
    assert listed[0]["name"] == "CI"


def test_key_label_must_carry_something(client):
    """The settings form marks the label required — the API agrees."""
    assert client.post("/api/apikeys", json={"name": "   "}).status_code == 422
    assert client.post("/api/apikeys", json={"name": ""}).status_code == 422
    assert client.get("/api/apikeys").json() == []

    created = client.post("/api/apikeys", json={"name": "  padded  "})
    assert created.status_code == 201
    assert created.json()["name"] == "padded"


def test_delete_key(client):
    created = client.post("/api/apikeys", json={"name": "tmp"}).json()
    assert client.delete(f"/api/apikeys/{created['id']}").json() == {"deleted": True}
    assert client.delete(f"/api/apikeys/{created['id']}").status_code == 404
    assert client.get("/api/apikeys").json() == []


# ── authentication ────────────────────────────────────────────────────


def test_open_without_configured_keys(client):
    response = post_audio(client)
    assert response.status_code == 200
    assert response.json() == {"text": "Hallo Welt.\nZweiter Satz."}


def test_requires_bearer_once_a_key_exists(client):
    key = client.post("/api/apikeys", json={"name": "k"}).json()["key"]

    assert post_audio(client).status_code == 401
    assert post_audio(client, headers={"Authorization": "Bearer vb-wrong"}).status_code == 401

    ok = post_audio(client, headers={"Authorization": f"Bearer {key}"})
    assert ok.status_code == 200

    listed = client.get("/api/apikeys").json()
    assert listed[0]["last_used_at"]  # verified use is recorded


# ── response formats ──────────────────────────────────────────────────


def test_text_format(client):
    response = post_audio(client, data={"response_format": "text"})
    assert response.status_code == 200
    assert response.text == "Hallo Welt.\nZweiter Satz."


def test_srt_format(client):
    response = post_audio(client, data={"response_format": "srt"})
    assert "1\n00:00:00,000 --> 00:00:02,500\nHallo Welt." in response.text
    assert "2\n00:00:02,500 --> 00:01:01,200\nZweiter Satz." in response.text


def test_vtt_format(client):
    response = post_audio(client, data={"response_format": "vtt"})
    assert response.text.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.500\nHallo Welt." in response.text


def test_verbose_json_format(client):
    data = post_audio(client, data={"response_format": "verbose_json"}).json()
    assert data["task"] == "transcribe"
    assert data["language"] == "de"
    assert data["duration"] == 61.2
    assert data["segments"] == [
        {"id": 0, "start": 0.0, "end": 2.5, "text": "Hallo Welt."},
        {"id": 1, "start": 2.5, "end": 61.2, "text": "Zweiter Satz."},
    ]


def test_unknown_response_format_rejected(client):
    response = post_audio(client, data={"response_format": "xml"})
    assert response.status_code == 400


def test_language_parameter_is_passed_through(client):
    data = post_audio(client, data={"response_format": "verbose_json", "language": "en"}).json()
    assert data["language"] == "en"


# ── optional cleanup ──────────────────────────────────────────────────


def test_model_plus_cleanup_runs_llm(client, monkeypatch):
    monkeypatch.setattr("verba.services.llm.llm_location", lambda *a, **k: "remote")
    monkeypatch.setattr(
        "verba.services.llm.chat", lambda messages, **kwargs: "[CLEAN] " + messages[-1]["content"]
    )
    response = post_audio(client, data={"model": "whisper-1+cleanup"})
    assert response.status_code == 200
    assert response.json()["text"].startswith("[CLEAN] ")


def test_project_type_implies_cleanup_with_type_prompt(client, monkeypatch):
    seen = {}

    def fake_chat(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        return "bereinigt"

    monkeypatch.setattr("verba.services.llm.llm_location", lambda *a, **k: "remote")
    monkeypatch.setattr("verba.services.llm.chat", fake_chat)
    response = post_audio(client, data={"project_type": "interview"})
    assert response.status_code == 200
    assert response.json()["text"] == "bereinigt"
    assert "Interview" in seen["system"]  # built-in type prompt was attached


def test_cleanup_without_llm_rejected(client):
    response = post_audio(client, data={"model": "whisper-1+cleanup"})
    assert response.status_code == 400
    assert "LLM" in response.json()["detail"]


def test_unknown_project_type_rejected(client):
    response = post_audio(client, data={"project_type": "gibt-es-nicht"})
    assert response.status_code == 400


def test_verbose_json_keeps_raw_segments_with_cleanup(client, monkeypatch):
    """Timestamps stay meaningful: segments are raw, only `text` is cleaned."""
    monkeypatch.setattr("verba.services.llm.llm_location", lambda *a, **k: "remote")
    monkeypatch.setattr("verba.services.llm.chat", lambda messages, **kwargs: "bereinigt")
    data = post_audio(
        client, data={"model": "small+cleanup", "response_format": "verbose_json"}
    ).json()
    assert data["text"] == "bereinigt"
    assert data["segments"][0]["text"] == "Hallo Welt."


# ── plumbing ──────────────────────────────────────────────────────────


def test_api_transcribe_runs_in_main_lane():
    assert lane_for_kind("api_transcribe") == "main"


def test_upload_is_deleted_after_the_job(client):
    post_audio(client)
    assert list(public_api.uploads_dir().iterdir()) == []


def test_failed_job_returns_500(client, monkeypatch):
    def boom(audio_path, **kwargs):
        raise RuntimeError("Kaputtes Audio")

    monkeypatch.setattr(whisper, "transcribe_path", boom)
    response = post_audio(client)
    assert response.status_code == 500
    assert "Kaputtes Audio" in response.json()["detail"]


def test_timestamp_formatting():
    assert public_api.format_timestamp(0, comma=True) == "00:00:00,000"
    assert public_api.format_timestamp(3661.5, comma=True) == "01:01:01,500"
    assert public_api.format_timestamp(59.999, comma=False) == "00:00:59.999"
