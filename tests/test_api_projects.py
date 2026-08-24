from __future__ import annotations

import io
from pathlib import Path

import pytest

from verba import config


@pytest.fixture(autouse=True)
def _workspaces_in_tmp(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.general.browse_roots = [str(tmp_path)]
    config.save_settings(settings)


def test_project_crud(client):
    created = client.post("/api/projects", json={"name": "Interviews"}).json()
    assert created["slug"] == "interviews"

    listed = client.get("/api/projects").json()
    assert len(listed) == 1
    assert listed[0]["file_count"] == 0

    detail = client.get(f"/api/projects/{created['id']}").json()
    assert detail["files"] == []

    assert client.delete(f"/api/projects/{created['id']}").status_code == 200
    assert client.get(f"/api/projects/{created['id']}").status_code == 404


def test_create_project_requires_name(client):
    assert client.post("/api/projects", json={"name": ""}).status_code == 422


def test_upload_and_list(client):
    project = client.post("/api/projects", json={"name": "Upload"}).json()
    response = client.post(
        f"/api/projects/{project['id']}/files/upload",
        files={"file": ("rede.mp3", io.BytesIO(b"fake-audio"), "audio/mpeg")},
    )
    assert response.status_code == 200
    file_row = response.json()
    assert file_row["filename"] == "rede.mp3"
    assert file_row["status"] == "pending"

    detail = client.get(f"/api/projects/{project['id']}").json()
    assert len(detail["files"]) == 1


def test_upload_rejects_non_audio(client):
    project = client.post("/api/projects", json={"name": "Falsch"}).json()
    response = client.post(
        f"/api/projects/{project['id']}/files/upload",
        files={"file": ("notes.txt", io.BytesIO(b"text"), "text/plain")},
    )
    assert response.status_code == 422


def test_upload_strips_directory_parts(client, tmp_path):
    project = client.post("/api/projects", json={"name": "Traversal"}).json()
    response = client.post(
        f"/api/projects/{project['id']}/files/upload",
        files={"file": ("..\\..\\boese.mp3", io.BytesIO(b"x"), "audio/mpeg")},
    )
    assert response.status_code == 200
    stored = Path(project["workspace"]) / "audio"
    files = [p.name for p in stored.iterdir()]
    assert files == ["boese.mp3"]


def test_import_endpoint(client, tmp_path):
    source = tmp_path / "quelle"
    source.mkdir()
    (source / "a.mp3").write_bytes(b"1")
    project = client.post("/api/projects", json={"name": "Import"}).json()

    response = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(source)]}
    )
    assert response.status_code == 200
    assert [f["filename"] for f in response.json()] == ["a.mp3"]

    # folder without audio → 422
    empty = tmp_path / "leer"
    empty.mkdir()
    response = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(empty)]}
    )
    assert response.status_code == 422


def test_transcribe_options_reach_job_payload(client, tmp_path, monkeypatch):
    import json

    from verba.core.jobs import job_queue

    # keep the worker from running the real whisper handler (no network in tests)
    monkeypatch.setitem(job_queue._handlers, "transcribe", lambda job, cancel, report: None)

    (tmp_path / "opt.mp3").write_bytes(b"1")
    project = client.post("/api/projects", json={"name": "Optionen"}).json()
    [file_row] = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(tmp_path / "opt.mp3")]}
    ).json()

    job = client.post(
        f"/api/files/{file_row['id']}/transcribe",
        json={"model": "tiny", "language": "de"},
    ).json()
    payload = json.loads(job["payload"])
    assert payload == {"model": "tiny", "language": "de"}

    # empty options are omitted from the payload
    file2 = client.post(
        f"/api/projects/{project['id']}/files/upload",
        files={"file": ("zwei.mp3", io.BytesIO(b"2"), "audio/mpeg")},
    ).json()
    job2 = client.post(f"/api/files/{file2['id']}/transcribe", json={"model": ""}).json()
    assert json.loads(job2["payload"]) == {}


def test_segments_endpoint_empty(client, tmp_path):
    (tmp_path / "x.mp3").write_bytes(b"1")
    project = client.post("/api/projects", json={"name": "Seg"}).json()
    [file_row] = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(tmp_path / "x.mp3")]}
    ).json()
    data = client.get(f"/api/files/{file_row['id']}/segments").json()
    assert data["segments"] == []
