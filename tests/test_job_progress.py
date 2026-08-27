"""What the UI needs to answer "which step, which file, how far".

Every job row travels with the name of its file, project-wide work reports
which file it is on, and the messages that reach the web UI are German (the
project rule for user-visible backend texts).
"""

from __future__ import annotations

import re
import threading

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import pipeline, project_types, vectorstore, workspace

NO_CANCEL = threading.Event()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    project_types.seed_builtin_types()
    # enqueue() requires a registered handler, and a real one would start a
    # transcription (model download) in tests
    for kind in ("transcribe", "export_pdf", "llm_process", "reindex_search"):
        monkeypatch.setitem(job_queue._handlers, kind, lambda job, cancel, report: None)
    return tmp_path


def make_file(env, name="lied.mp3", status="done"):
    project = workspace.create_project("Lieder")
    source = env / name
    source.write_bytes(b"x")
    [row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(row["id"], status)
    return workspace.get_project(project["id"]), workspace.get_file(row["id"])


# ── the file name travels with the job ────────────────────────────────


def test_a_job_row_carries_the_file_name(env):
    _, file_row = make_file(env)
    job = job_queue.enqueue(
        "transcribe", payload={}, file_id=file_row["id"], project_id=file_row["project_id"]
    )
    assert job["filename"] == "lied.mp3"
    assert job_queue.get(job["id"])["filename"] == "lied.mp3"


def test_the_websocket_event_carries_it_too(env, monkeypatch):
    """The top bar composes its line from this event alone."""
    from verba.core import jobs as jobs_module

    events: list[dict] = []
    monkeypatch.setattr(
        jobs_module.hub,
        "publish",
        lambda event_type, data=None: events.append({"type": event_type, "data": data}),
    )
    _, file_row = make_file(env)
    job_queue.enqueue("transcribe", payload={}, file_id=file_row["id"])

    [event] = [e for e in events if e["type"] == "job.update"]
    assert event["data"]["filename"] == "lied.mp3"
    assert event["data"]["kind"] == "transcribe"


def test_a_job_without_a_file_has_no_name(env):
    project, _ = make_file(env)
    job = job_queue.enqueue("export_pdf", payload={"scope": "project"}, project_id=project["id"])
    assert job["filename"] is None
    assert job["project_id"] == project["id"]  # the transcript view can still show it


def test_the_listing_and_the_queue_overview_agree(env):
    _, file_row = make_file(env)
    job_queue.enqueue("transcribe", payload={}, file_id=file_row["id"])

    listed = job_queue.list_jobs(active_only=True)
    overview = job_queue.queue_overview()

    assert [j["filename"] for j in listed] == ["lied.mp3"]
    assert [j["filename"] for j in overview["lanes"]["main"]] == ["lied.mp3"]


def test_the_api_exposes_the_name(client, env):
    _, file_row = make_file(env)
    job_queue.enqueue("transcribe", payload={}, file_id=file_row["id"])
    [job] = client.get("/api/jobs", params={"active": True}).json()
    assert job["filename"] == "lied.mp3"


# ── progress messages ─────────────────────────────────────────────────


def collect_messages(handler, job) -> list[tuple[int, str]]:
    seen: list[tuple[int, str]] = []
    handler(job, NO_CANCEL, lambda percent, message="": seen.append((percent, message)))
    return seen


def test_a_project_export_names_the_file_it_is_on(env, monkeypatch):
    """One job produces one PDF, so the message is the only place to say it."""
    from verba.services import pdf

    project, file_row = make_file(env, "20240817_Sommerlied.mp3")
    pipeline.save_text(file_row["id"], "cleanup", "Zeile")
    monkeypatch.setattr(pdf, "render_pdf", lambda docs, structure, target: target.touch())

    messages = collect_messages(
        pdf.handle_export_job, {"payload": {"scope": "project", "project_id": project["id"]}}
    )

    assert any("20240817_Sommerlied.mp3" in message for _, message in messages)
    assert any("Datei 1/1" in message for _, message in messages)
    assert messages[-1][0] == 100


def test_cleanup_and_translation_report_their_chunk(env, monkeypatch):
    monkeypatch.setattr(pipeline.llm, "chat", lambda messages, **kw: "Text")
    segments = [{"speaker": "", "text": "Hallo Welt.", "start_s": 0.0, "end_s": 2.0}]
    seen: list[tuple[int, str]] = []

    _, file_row = make_file(env)
    pipeline.cleanup_segments(segments, "", "", NO_CANCEL, lambda p, m: seen.append((p, m)))
    pipeline.run_translation(
        file_row["id"], "Hallo", "en", "", NO_CANCEL, lambda p, m: seen.append((p, m))
    )

    assert any(re.match(r"Bereinigung \d+/\d+", message) for _, message in seen)
    assert any(re.match(r"Übersetzung \d+/\d+", message) for _, message in seen)


def test_reindex_reports_per_file_and_in_german(env, monkeypatch):
    monkeypatch.setattr(vectorstore, "index_file", lambda file_id: 3)
    monkeypatch.setattr(vectorstore, "unload_model", lambda: None)
    from contextlib import contextmanager

    @contextmanager
    def fake_conn():
        with db.get_conn() as conn:
            yield conn

    monkeypatch.setattr(vectorstore, "_vec_conn", fake_conn)
    make_file(env)

    messages = collect_messages(vectorstore.handle_reindex_job, {"payload": {}})

    assert any(message.startswith("Datei 1/1") for _, message in messages)
    assert messages[-1][1].startswith("Neu indiziert:")


def test_no_english_left_in_the_progress_messages():
    """These strings are shown in the web UI, so they belong in German."""
    from pathlib import Path

    english = re.compile(
        r'report\(\s*[^,]+,\s*f?"[^"]*\b(File|Processing|Transcription|Indexing|Generating'
        r"|Reindexed|is running|New file|unavailable)\b",
    )
    offenders = []
    for path in Path("backend/verba").rglob("*.py"):
        for match in english.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}: {match.group(0)[-60:]}")
    assert not offenders, offenders
