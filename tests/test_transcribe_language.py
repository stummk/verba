"""Which language a file is transcribed in, and what a selection gives back.

Two things the editor depends on:

- The language is *stated*, not guessed, whenever anything states it. Whisper's
  detector hears the first seconds and does get them wrong — a Russian
  recording read as German is transcribed, cleaned up and "translated" in the
  wrong language, and nothing downstream can notice.
- Transcribing a selection hands back text and writes no segments; only the
  `transcribe` job over the whole file rebuilds them.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import audio, transcripts, whisper, workspace

NO_CANCEL = threading.Event()


def noop_report(percent: int, message: str = "") -> None:
    pass


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start, self.end, self.text = start, end, text


class FakeInfo:
    def __init__(self, language: str, duration: float) -> None:
        self.language, self.duration = language, duration


class FakeModel:
    """Records the language it was asked for and always "hears" German."""

    def __init__(self, segments: list[FakeSegment]) -> None:
        self.segments = segments
        self.asked_for: object = "not called"

    def transcribe(self, path, language=None, beam_size=5):
        self.asked_for = language
        return iter(self.segments), FakeInfo("de", 6.0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.whisper.language = ""
    config.save_settings(settings)
    db.init_db()
    # the follow-up jobs a finished transcription enqueues must not run here
    for kind in ("index_file", "llm_process"):
        monkeypatch.setitem(job_queue._handlers, kind, lambda job, cancel, report: None)
    return tmp_path


@pytest.fixture()
def file_row(env):
    project = workspace.create_project("Predigten")
    source = env / "20260731_ru_de_Wesner.mp3"
    source.write_bytes(b"x")
    [row] = workspace.import_paths(project, [str(source)])
    return workspace.get_file(row["id"])


def install_model(monkeypatch, segments: list[FakeSegment]) -> FakeModel:
    model = FakeModel(segments)
    monkeypatch.setattr(whisper, "get_model", lambda model_override="": model)
    monkeypatch.setattr(whisper, "_publish_engine_status", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "_publish_engine_status_running", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "probe_duration", lambda path: 6.0)
    return model


# ── which language wins ───────────────────────────────────────────────


def test_the_file_name_decides_over_the_detector(file_row):
    """`20260731_ru_de_…` states Russian — that is what is transcribed."""
    assert file_row["language"] == "ru"
    assert whisper.language_for(file_row) == "ru"


def test_a_run_option_outranks_the_file(file_row):
    assert whisper.language_for(file_row, {"language": "uk"}) == "uk"


def test_the_setting_fills_in_for_a_file_that_says_nothing(env):
    settings = config.get_settings()
    settings.whisper.language = "en"
    config.save_settings(settings)
    assert whisper.language_for({"language": ""}) == "en"


def test_nothing_stated_means_automatic_detection(env):
    assert whisper.language_for({"language": ""}) == ""


# ── the whole file ────────────────────────────────────────────────────


def test_transcription_asks_for_the_declared_language(file_row, monkeypatch):
    model = install_model(monkeypatch, [FakeSegment(0.0, 2.0, "Привет")])
    job = {"file_id": file_row["id"], "payload": {}}

    whisper.handle_transcribe_job(job, NO_CANCEL, noop_report)

    assert model.asked_for == "ru"


def test_a_declared_language_survives_the_detector(file_row, monkeypatch):
    """The model reports German; the file stays Russian, as its name says."""
    install_model(monkeypatch, [FakeSegment(0.0, 2.0, "Привет")])

    whisper.handle_transcribe_job(
        {"file_id": file_row["id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert workspace.get_file(file_row["id"])["language"] == "ru"


def test_detection_still_fills_a_file_that_declares_nothing(file_row, monkeypatch):
    workspace.update_file(file_row["id"], {"language": ""})
    install_model(monkeypatch, [FakeSegment(0.0, 2.0, "Guten Tag")])

    whisper.handle_transcribe_job(
        {"file_id": file_row["id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert workspace.get_file(file_row["id"])["language"] == "de"


def test_transcribing_the_file_again_rebuilds_the_segments(file_row, monkeypatch):
    install_model(monkeypatch, [FakeSegment(0.0, 2.0, "eins"), FakeSegment(2.0, 4.0, "zwei")])
    job = {"file_id": file_row["id"], "payload": {}}

    whisper.handle_transcribe_job(job, NO_CANCEL, noop_report)
    whisper.handle_transcribe_job(job, NO_CANCEL, noop_report)  # a second run replaces, not appends

    texts = [s["text"] for s in transcripts.list_segments(file_row["id"])]
    assert texts == ["eins", "zwei"]


# ── the selection ─────────────────────────────────────────────────────


@pytest.fixture()
def range_job(file_row, monkeypatch):
    monkeypatch.setattr(
        audio, "extract_range", lambda source, start_s, end_s, target: Path(target).write_bytes(b"")
    )
    return {
        "file_id": file_row["id"],
        "payload": {"ranges": [[1.0, 3.0]]},
    }


def test_a_transcribed_selection_writes_no_segments(range_job, monkeypatch):
    install_model(
        monkeypatch, [FakeSegment(0.0, 1.0, "Das ist"), FakeSegment(1.0, 2.0, "der Text")]
    )

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    assert transcripts.list_segments(range_job["file_id"]) == []


def test_a_transcribed_selection_publishes_its_text(range_job, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        whisper.hub,
        "publish",
        lambda event_type, data=None, **scope: events.append({"type": event_type, "data": data}),
    )
    install_model(
        monkeypatch, [FakeSegment(0.0, 1.0, "Das ist"), FakeSegment(1.0, 2.0, "der Text")]
    )

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    [event] = [e for e in events if e["type"] == "range.text"]
    assert event["data"]["text"] == "Das ist der Text"
    assert event["data"]["file_id"] == range_job["file_id"]
    assert (event["data"]["start_s"], event["data"]["end_s"]) == (1.0, 3.0)


def test_a_selection_is_transcribed_in_the_declared_language(range_job, monkeypatch):
    model = install_model(monkeypatch, [FakeSegment(0.0, 1.0, "Привет")])

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    assert model.asked_for == "ru"


def test_several_selections_are_transcribed_in_one_job(range_job, monkeypatch):
    """One job, one loaded model, one event per passage — the editor lists them."""
    range_job["payload"]["ranges"] = [[1.0, 3.0], [5.0, 6.0]]
    events: list[dict] = []
    monkeypatch.setattr(
        whisper.hub,
        "publish",
        lambda event_type, data=None, **scope: events.append({"type": event_type, "data": data}),
    )
    install_model(monkeypatch, [FakeSegment(0.0, 1.0, "Text")])

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    published = [e["data"] for e in events if e["type"] == "range.text"]
    assert [(p["start_s"], p["end_s"]) for p in published] == [(1.0, 3.0), (5.0, 6.0)]
    assert [p["index"] for p in published] == [0, 1]
    assert {p["total"] for p in published} == {2}


def test_a_silent_selection_says_so_in_german(range_job, monkeypatch):
    install_model(monkeypatch, [])
    messages: list[str] = []

    whisper.handle_transcribe_range_job(
        range_job, NO_CANCEL, lambda percent, message="": messages.append(message)
    )

    assert "kein Text erkannt" in messages[-1]
