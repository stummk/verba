from __future__ import annotations

import pytest

from verba import config
from verba.services import whisper


@pytest.fixture()
def local_model(tmp_path):
    root = config.models_dir(config.get_settings())
    folder = root / "mein-modell"
    folder.mkdir(parents=True)
    (folder / "model.bin").write_bytes(b"")
    return folder


def test_download_requires_valid_name(client):
    assert client.post("/api/models/download", json={"name": ""}).status_code == 422
    assert client.post("/api/models/download", json={"name": "../evil"}).status_code == 422
    assert client.post("/api/models/download", json={"name": "a/b/c"}).status_code == 422


def test_download_starts_thread(client, monkeypatch):
    started = {}
    monkeypatch.setattr(whisper, "start_model_download", lambda name: started.setdefault("n", name))
    response = client.post("/api/models/download", json={"name": "tiny"})
    assert response.status_code == 202
    assert started["n"] == "tiny"


def test_download_conflict_when_already_running(client, monkeypatch):
    monkeypatch.setattr(whisper, "start_model_download", lambda name: False)
    assert client.post("/api/models/download", json={"name": "tiny"}).status_code == 409


def test_delete_local_model(client, local_model):
    assert local_model.exists()
    response = client.delete("/api/models", params={"name": "mein-modell"})
    assert response.status_code == 200
    assert not local_model.exists()


def test_delete_rejects_traversal(client, local_model, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.bin").write_bytes(b"")
    response = client.delete("/api/models", params={"name": "../outside"})
    assert response.status_code == 403
    assert (outside / "model.bin").exists()


def test_delete_missing_model_404(client):
    assert client.delete("/api/models", params={"name": "gibtsnicht"}).status_code == 404


def test_list_models_includes_download_state(client):
    data = client.get("/api/models").json()
    assert "downloading" in data
    assert data["downloading"] == []
