from __future__ import annotations

import pytest

from verba import config


@pytest.fixture()
def browse_root(tmp_path):
    root = tmp_path / "erlaubt"
    (root / "unter").mkdir(parents=True)
    (root / "song.mp3").write_bytes(b"x")
    (root / "unter" / "geheim.txt").write_text("no audio")
    settings = config.get_settings()
    settings.general.browse_roots = [str(root)]
    config.save_settings(settings)
    return root


def test_browse_lists_roots_when_no_path(client, browse_root):
    data = client.get("/api/files/browse").json()
    assert data["path"] == ""
    assert [d["path"] for d in data["dirs"]] == [str(browse_root.resolve())]


def test_browse_lists_audio_and_dirs(client, browse_root):
    data = client.get("/api/files/browse", params={"path": str(browse_root)}).json()
    assert [d["name"] for d in data["dirs"]] == ["unter"]
    assert [f["name"] for f in data["files"]] == ["song.mp3"]


def test_browse_rejects_path_outside_roots(client, browse_root, tmp_path):
    outside = tmp_path / "verboten"
    outside.mkdir()
    response = client.get("/api/files/browse", params={"path": str(outside)})
    assert response.status_code == 403


def test_browse_rejects_traversal(client, browse_root):
    sneaky = str(browse_root / ".." / "..")
    response = client.get("/api/files/browse", params={"path": sneaky})
    assert response.status_code == 403


def test_parent_of_root_is_roots_overview(client, browse_root):
    data = client.get("/api/files/browse", params={"path": str(browse_root)}).json()
    assert data["parent"] == ""


def test_import_respects_roots(client, browse_root, tmp_path):
    outside = tmp_path / "aussen"
    outside.mkdir()
    (outside / "b.mp3").write_bytes(b"x")
    project = client.post("/api/projects", json={"name": "Sicher"}).json()
    response = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(outside / "b.mp3")]}
    )
    assert response.status_code == 403
