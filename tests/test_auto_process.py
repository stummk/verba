"""Automatic pipeline chaining after transcription and manual text editing."""

from __future__ import annotations

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
