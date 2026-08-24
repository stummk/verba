from __future__ import annotations

import json
from pathlib import Path

import pytest

from verba import config, db
from verba.services import transcripts, workspace


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()


@pytest.fixture()
def file_with_segments(tmp_path):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("Editor")
    [file_row] = workspace.import_paths(project, [str(source)])
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            [
                (file_row["id"], 0, 0.0, 2.0, "eins"),
                (file_row["id"], 1, 2.0, 4.0, "zwei"),
                (file_row["id"], 2, 4.0, 6.0, "drei"),
            ],
        )
    return file_row


def _transcript_json(file_row) -> dict:
    project = workspace.get_project(file_row["project_id"])
    path = Path(project["workspace"]) / "transcripts" / (Path(file_row["filename"]).stem + ".json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_update_segment_text_and_speaker(file_with_segments):
    segments = transcripts.list_segments(file_with_segments["id"])
    updated = transcripts.update_segment(
        segments[0]["id"], {"text": "korrigiert", "speaker": "Anna"}
    )
    assert updated["text"] == "korrigiert"
    assert updated["speaker"] == "Anna"

    # workspace JSON was rewritten
    data = _transcript_json(file_with_segments)
    assert data["segments"][0]["text"] == "korrigiert"
    assert data["segments"][0]["speaker"] == "Anna"


def test_update_ignores_unknown_fields(file_with_segments):
    segments = transcripts.list_segments(file_with_segments["id"])
    updated = transcripts.update_segment(segments[0]["id"], {"idx": 99, "file_id": 1})
    assert updated["idx"] == 0


def test_delete_segment_reindexes(file_with_segments):
    segments = transcripts.list_segments(file_with_segments["id"])
    assert transcripts.delete_segment(segments[1]["id"]) is True
    remaining = transcripts.list_segments(file_with_segments["id"])
    assert [s["text"] for s in remaining] == ["eins", "drei"]
    assert [s["idx"] for s in remaining] == [0, 1]


def test_replace_range_merges_overlapping(file_with_segments):
    file_id = file_with_segments["id"]
    # replace [1.5, 4.5] — overlaps "eins" (0-2), "zwei" (2-4) and "drei" (4-6)
    total = transcripts.replace_range(
        file_id, 1.5, 4.5, [{"start": 1.5, "end": 4.4, "text": "neu"}]
    )
    assert total == 1
    assert [s["text"] for s in transcripts.list_segments(file_id)] == ["neu"]


def test_replace_range_keeps_non_overlapping(file_with_segments):
    file_id = file_with_segments["id"]
    # replace exactly the middle segment (2-4)
    transcripts.replace_range(
        file_id,
        2.0,
        4.0,
        [{"start": 2.0, "end": 3.0, "text": "neu-a"}, {"start": 3.0, "end": 4.0, "text": "neu-b"}],
    )
    texts = [s["text"] for s in transcripts.list_segments(file_id)]
    assert texts == ["eins", "neu-a", "neu-b", "drei"]
    assert [s["idx"] for s in transcripts.list_segments(file_id)] == [0, 1, 2, 3]


def test_speaker_migration_on_legacy_db():
    with db.get_conn() as conn:
        conn.execute("DROP TABLE segments")
        conn.execute(
            "CREATE TABLE segments (id INTEGER PRIMARY KEY, file_id INTEGER, idx INTEGER, "
            "start_s REAL, end_s REAL, text TEXT, UNIQUE(file_id, idx))"
        )
    db.init_db()  # must add the speaker column
    with db.get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
    assert "speaker" in columns
