"""Automatic pipeline chaining after transcription and manual text editing."""

from __future__ import annotations

import json
import threading

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import pipeline, workspace


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    monkeypatch.setitem(job_queue._handlers, "llm_process", lambda job, cancel, report: None)


@pytest.fixture()
def done_file(tmp_path):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("Auto")
    [file_row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(file_row["id"], "done")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, 0, 0, 1, 'hi')",
            (file_row["id"],),
        )
    return workspace.get_file(file_row["id"])


def configure_llm():
    settings = config.get_settings()
    settings.llm.mode = "openai"
    settings.llm.base_url = "https://api.example.com/v1"
    settings.llm.model = "m"
    config.save_settings(settings)


def test_no_auto_job_when_disabled(done_file):
    configure_llm()
    assert pipeline.maybe_enqueue_auto_process(done_file["id"]) is None


def test_no_auto_job_without_llm(done_file):
    workspace.update_project(done_file["project_id"], {"auto_process": 1})
    assert pipeline.maybe_enqueue_auto_process(done_file["id"]) is None


def test_auto_job_cleanup_only(done_file):
    configure_llm()
    workspace.update_project(done_file["project_id"], {"auto_process": 1})
    job = pipeline.maybe_enqueue_auto_process(done_file["id"], session_id="s1")
    assert job is not None
    assert job["kind"] == "llm_process"
    assert job["session_id"] == "s1"
    assert '"steps": ["cleanup"]' in job["payload"]


def test_auto_job_with_translation(done_file):
    configure_llm()
    workspace.update_project(done_file["project_id"], {"auto_process": 1, "auto_language": "en"})
    job = pipeline.maybe_enqueue_auto_process(done_file["id"])
    assert '"translate"' in job["payload"]
    assert '"target_language": "en"' in job["payload"]


def test_project_update_endpoint_is_partial(client):
    project = client.post("/api/projects", json={"name": "P"}).json()
    updated = client.put(
        f"/api/projects/{project['id']}", json={"auto_process": True, "auto_language": "ru"}
    ).json()
    assert updated["auto_process"] == 1
    assert updated["auto_language"] == "ru"
    assert updated["type_id"] is None  # untouched — partial update

    # sending only type_id must not reset the auto settings
    types = client.get("/api/types").json()
    updated = client.put(f"/api/projects/{project['id']}", json={"type_id": types[0]["id"]}).json()
    assert updated["auto_process"] == 1
    assert updated["auto_language"] == "ru"


def test_edit_derived_text(done_file, client):
    pipeline.save_text(done_file["id"], "cleanup", "Original", model="m")
    response = client.put(
        f"/api/files/{done_file['id']}/texts/cleanup", json={"content": "Editiert"}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "Editiert"
    assert response.json()["model"] == "m"  # attribution preserved

    # workspace markdown copy follows the edit
    project = workspace.get_project(done_file["project_id"])
    from pathlib import Path

    md = Path(project["workspace"]) / "transcripts" / "a.cleanup.md"
    assert md.read_text(encoding="utf-8") == "Editiert"


def test_edit_missing_text_404(done_file, client):
    response = client.put(
        f"/api/files/{done_file['id']}/texts/translation?language=en",
        json={"content": "x"},
    )
    assert response.status_code == 404


def test_auto_job_does_not_duplicate_a_manual_run(done_file):
    """The user already started the cleanup by hand — one job is enough."""
    configure_llm()
    workspace.update_project(done_file["project_id"], {"auto_process": 1})
    manual = job_queue.enqueue(
        "llm_process", payload={"file_id": done_file["id"]}, file_id=done_file["id"]
    )
    assert pipeline.maybe_enqueue_auto_process(done_file["id"])["id"] == manual["id"]


def test_process_endpoint_returns_the_running_job_instead_of_a_second_one(
    done_file, client, monkeypatch
):
    """Clicking twice must not queue the same step twice (the app registers the
    real handler, so the job is held here until both requests are through)."""
    configure_llm()
    gate = threading.Event()
    monkeypatch.setitem(
        job_queue._handlers, "llm_process", lambda job, cancel, report: gate.wait(10)
    )
    try:
        first = client.post(f"/api/files/{done_file['id']}/process", json={"steps": ["cleanup"]})
        second = client.post(f"/api/files/{done_file['id']}/process", json={"steps": ["cleanup"]})
        assert first.status_code == 200
        assert second.json()["id"] == first.json()["id"]
    finally:
        gate.set()


def test_translation_starts_while_the_cleanup_is_still_running(done_file, client, monkeypatch):
    """A step nobody is working on must start — even next to a running one.

    The dedup guard used to hand back *any* running AI job for the file, so a
    translation requested during a cleanup was answered with "started" and then
    never produced.
    """
    configure_llm()
    gate = threading.Event()
    monkeypatch.setitem(
        job_queue._handlers, "llm_process", lambda job, cancel, report: gate.wait(10)
    )
    try:
        cleanup = client.post(f"/api/files/{done_file['id']}/process", json={"steps": ["cleanup"]})
        translate = client.post(
            f"/api/files/{done_file['id']}/process",
            json={"steps": ["translate"], "target_language": "en"},
        )
        assert translate.status_code == 200
        assert translate.json()["id"] != cleanup.json()["id"]
        # the second job carries the translation only — the cleanup is on its way
        assert json.loads(translate.json()["payload"])["steps"] == ["translate"]
    finally:
        gate.set()


def test_a_second_language_is_not_swallowed_by_a_running_translation(
    done_file, client, monkeypatch
):
    """Translations are deduplicated per language, not per file."""
    configure_llm()
    gate = threading.Event()
    monkeypatch.setitem(
        job_queue._handlers, "llm_process", lambda job, cancel, report: gate.wait(10)
    )
    body = {"steps": ["translate"], "target_language": "en"}
    try:
        first = client.post(f"/api/files/{done_file['id']}/process", json=body)
        same = client.post(f"/api/files/{done_file['id']}/process", json=body)
        other = client.post(
            f"/api/files/{done_file['id']}/process",
            json={"steps": ["translate"], "target_language": "ru"},
        )
        assert same.json()["id"] == first.json()["id"]  # same language: one run
        assert other.json()["id"] != first.json()["id"]  # other language: own run
    finally:
        gate.set()


def test_auto_process_still_adds_the_step_the_manual_run_left_out(done_file):
    """A manual cleanup does not cancel the automatic translation."""
    configure_llm()
    workspace.update_project(done_file["project_id"], {"auto_process": 1, "auto_language": "en"})
    manual = job_queue.enqueue(
        "llm_process",
        payload={"file_id": done_file["id"], "steps": ["cleanup"]},
        file_id=done_file["id"],
    )
    auto = pipeline.maybe_enqueue_auto_process(done_file["id"])
    assert auto["id"] != manual["id"]
    assert json.loads(auto["payload"])["steps"] == ["translate"]


def test_file_row_names_its_derived_texts(done_file):
    """The UI can only show "already cleaned" when the row says so."""
    assert workspace.get_file(done_file["id"])["derived_kinds"] is None
    pipeline.save_text(done_file["id"], "cleanup", "Bereinigt")
    assert workspace.get_file(done_file["id"])["derived_kinds"] == "cleanup"
    # an empty text is a failed run and must not count as done
    pipeline.save_text(done_file["id"], "translation", "   ", language="en")
    kinds = workspace.list_files(done_file["project_id"])[0]["derived_kinds"]
    assert kinds == "cleanup"
