from __future__ import annotations

import json
from pathlib import Path

import pytest

from verba import config, db
from verba.services import workspace


@pytest.fixture(autouse=True)
def _init_db(isolated_data_dir, monkeypatch, tmp_path):
    # keep workspaces inside the tmp dir, away from the repo
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()


def test_slugify():
    assert workspace.slugify("Mein Projekt 2026!") == "mein-projekt-2026"
    assert workspace.slugify("Übung & Prüfung") == "ubung-prufung"
    assert workspace.slugify("###") == "projekt"


def test_create_project_builds_workspace():
    project = workspace.create_project("Reden März")
    ws = Path(project["workspace"])
    assert ws.is_dir()
    for sub in ("audio", "transcripts", "exports"):
        assert (ws / sub).is_dir()
    meta = json.loads((ws / "project.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Reden März"


def test_duplicate_names_get_unique_slugs():
    first = workspace.create_project("Demo")
    second = workspace.create_project("Demo")
    assert first["slug"] == "demo"
    assert second["slug"] == "demo-2"


def test_import_folder_copies_only_audio(tmp_path):
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "a.mp3").write_bytes(b"x")
    (source / "sub" / "b.wav").write_bytes(b"y")
    (source / "notes.txt").write_text("kein audio")

    project = workspace.create_project("Import")
    imported = workspace.import_paths(project, [str(source)])

    assert sorted(f["filename"] for f in imported) == ["a.mp3", "b.wav"]
    audio_dir = Path(project["workspace"]) / "audio"
    assert (audio_dir / "a.mp3").exists()
    assert (audio_dir / "b.wav").exists()
    assert not (audio_dir / "notes.txt").exists()


def test_import_deduplicates_filenames(tmp_path):
    folder_a = tmp_path / "a"
    folder_b = tmp_path / "b"
    folder_a.mkdir()
    folder_b.mkdir()
    (folder_a / "gleich.mp3").write_bytes(b"1")
    (folder_b / "gleich.mp3").write_bytes(b"2")

    project = workspace.create_project("Dedupe")
    imported = workspace.import_paths(project, [str(folder_a), str(folder_b)])
    names = sorted(f["filename"] for f in imported)
    assert names == ["gleich-2.mp3", "gleich.mp3"]


def test_delete_file_removes_copy_not_source(tmp_path):
    source = tmp_path / "orig.mp3"
    source.write_bytes(b"data")
    project = workspace.create_project("Löschen")
    [file_row] = workspace.import_paths(project, [str(source)])

    workspace.delete_file(file_row["id"])
    assert workspace.get_file(file_row["id"]) is None
    assert source.exists()  # original untouched
    assert not workspace.file_path(file_row).exists()


def test_delete_project_removes_project_files_and_folder_by_default():
    project = workspace.create_project("Weg")
    source = Path(project["workspace"]).parent / "source.mp3"
    source.write_bytes(b"data")
    [file_row] = workspace.import_paths(project, [str(source)])

    workspace.delete_project(project["id"])
    assert workspace.get_project(project["id"]) is None
    assert workspace.get_file(file_row["id"]) is None
    assert not Path(project["workspace"]).exists()

    project2 = workspace.create_project("Bleibt")
    workspace.delete_project(project2["id"], delete_files=False)
    assert workspace.get_project(project2["id"]) is None
    assert Path(project2["workspace"]).is_dir()


def test_rename_project_moves_workspace_directory_and_updates_db():
    project = workspace.create_project("Alt")
    old_dir = Path(project["workspace"])
    old_dir.mkdir(exist_ok=True)

    renamed = workspace.update_project(project["id"], {"name": "Neu"})
    assert renamed is not None
    assert renamed["name"] == "Neu"
    assert renamed["slug"] == "neu"
    assert renamed["workspace"] == str(old_dir.parent / "neu")
    assert not old_dir.exists()
    assert Path(renamed["workspace"]).is_dir()

    meta = json.loads((Path(renamed["workspace"]) / "project.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Neu"
    assert meta["slug"] == "neu"
