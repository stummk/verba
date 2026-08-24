from __future__ import annotations

import json
from pathlib import Path

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services.audio import build_edit_command


@pytest.fixture(autouse=True)
def _workspaces_in_tmp(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.general.browse_roots = [str(tmp_path)]
    config.save_settings(settings)


@pytest.fixture()
def file_row(client, tmp_path, monkeypatch):
    # block real job execution — these tests only check API + queue payloads
    monkeypatch.setitem(job_queue._handlers, "transcribe", lambda *a: None)
    monkeypatch.setitem(job_queue._handlers, "transcribe_range", lambda *a: None)
    monkeypatch.setitem(job_queue._handlers, "audio_edit", lambda *a: None)

    (tmp_path / "a.mp3").write_bytes(b"x")
    project = client.post("/api/projects", json={"name": "Seg"}).json()
    [row] = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(tmp_path / "a.mp3")]}
    ).json()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) "
            "VALUES (?, 0, 0, 2, 'hallo')",
            (row["id"],),
        )
    return row


def _segment_id(client, file_id: int) -> int:
    return client.get(f"/api/files/{file_id}/segments").json()["segments"][0]["id"]


def test_update_segment_endpoint(client, file_row):
    segment_id = _segment_id(client, file_row["id"])
    response = client.put(f"/api/segments/{segment_id}", json={"text": "neu", "speaker": "Max"})
    assert response.status_code == 200
    assert response.json()["text"] == "neu"
    assert response.json()["speaker"] == "Max"


def test_update_missing_segment_404(client, file_row):
    assert client.put("/api/segments/99999", json={"text": "x"}).status_code == 404


def test_delete_segment_endpoint(client, file_row):
    segment_id = _segment_id(client, file_row["id"])
    assert client.delete(f"/api/segments/{segment_id}").status_code == 200
    assert client.get(f"/api/files/{file_row['id']}/segments").json()["segments"] == []


def test_transcribe_range_enqueues_payload(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/transcribe-range",
        json={"start_s": 1.0, "end_s": 3.5, "language": "de"},
    )
    assert response.status_code == 200
    payload = json.loads(response.json()["payload"])
    assert payload == {"start_s": 1.0, "end_s": 3.5, "language": "de"}


def test_transcribe_range_rejects_invalid_span(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/transcribe-range", json={"start_s": 3.0, "end_s": 1.0}
    )
    assert response.status_code == 422


def test_audio_edit_enqueues_job(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/audio/edit",
        json={"op": "trim", "start_s": 0.5, "end_s": 2.0},
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "audio_edit"


def test_audio_edit_rejects_unknown_op(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/audio/edit",
        json={"op": "explode", "start_s": 0.5, "end_s": 2.0},
    )
    assert response.status_code == 422


# ── ffmpeg command construction (pure) ────────────────────────────────


def test_build_trim_command():
    cmd = build_edit_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), "trim", 1.0, 5.0, 60.0)
    assert cmd[:6] == ["ffmpeg", "-y", "-ss", "1.000", "-to", "5.000"]


def test_build_cut_command_uses_concat_filter():
    cmd = build_edit_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), "cut", 10.0, 20.0, 60.0)
    joined = " ".join(cmd)
    assert "atrim=end=10.000" in joined
    assert "atrim=start=20.000" in joined
    assert "concat=n=2" in joined


def test_build_cut_at_file_start_degrades_to_trim():
    cmd = build_edit_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), "cut", 0.0, 5.0, 60.0)
    assert "-filter_complex" not in cmd
    assert cmd[2:4] == ["-ss", "5.000"]


def test_build_cut_at_file_end_degrades_to_trim():
    cmd = build_edit_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), "cut", 50.0, 60.0, 60.0)
    assert "-filter_complex" not in cmd
    assert cmd[2:4] == ["-to", "50.000"]
