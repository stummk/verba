from __future__ import annotations

import json
from pathlib import Path

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services.audio import build_keep_command


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
    monkeypatch.setitem(job_queue._handlers, "audio_restore", lambda *a: None)

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


def test_update_file_header_endpoint(client, file_row):
    response = client.put(
        f"/api/files/{file_row['id']}/header",
        json={"header_left": "Protokoll", "header_middle": "Sitzung", "header_right": "TOP 1"},
    )
    assert response.status_code == 200
    assert response.json()["header_left"] == "Protokoll"
    saved = client.get(f"/api/files/{file_row['id']}/segments").json()
    assert saved["file"]["header_middle"] == "Sitzung"


def test_update_file_header_rejects_too_long_value(client, file_row):
    response = client.put(f"/api/files/{file_row['id']}/header", json={"header_left": "x" * 501})
    assert response.status_code == 422


def test_create_segment_endpoint(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/segments",
        json={"start_s": 4.0, "end_s": 6.0, "text": "", "speaker": ""},
    )
    assert response.status_code == 200
    assert response.json()["text"] == ""
    listed = client.get(f"/api/files/{file_row['id']}/segments").json()["segments"]
    assert [s["start_s"] for s in listed] == [0.0, 4.0]


def test_create_segment_sorts_into_the_transcript(client, file_row):
    # a passage before the existing segment takes its place, not the end
    client.post(
        f"/api/files/{file_row['id']}/segments",
        json={"start_s": 5.0, "end_s": 6.0, "text": "spaet"},
    )
    client.post(
        f"/api/files/{file_row['id']}/segments",
        json={"start_s": 3.0, "end_s": 4.0, "text": "mitte"},
    )
    listed = client.get(f"/api/files/{file_row['id']}/segments").json()["segments"]
    assert [s["text"] for s in listed] == ["hallo", "mitte", "spaet"]
    assert [s["idx"] for s in listed] == [0, 1, 2]


def test_create_segment_rejects_invalid_span(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/segments", json={"start_s": 3.0, "end_s": 3.0}
    )
    assert response.status_code == 422


def test_create_segment_on_foreign_file_404(client):
    response = client.post("/api/files/99999/segments", json={"start_s": 0, "end_s": 1})
    assert response.status_code == 404


def test_update_missing_segment_404(client, file_row):
    assert client.put("/api/segments/99999", json={"text": "x"}).status_code == 404


def test_delete_segment_endpoint(client, file_row):
    segment_id = _segment_id(client, file_row["id"])
    assert client.delete(f"/api/segments/{segment_id}").status_code == 200
    assert client.get(f"/api/files/{file_row['id']}/segments").json()["segments"] == []


def test_transcribe_range_enqueues_the_selected_passages(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/transcribe-range",
        json={"ranges": [{"start_s": 1.0, "end_s": 3.5}], "language": "de"},
    )
    assert response.status_code == 200
    payload = json.loads(response.json()["payload"])
    assert payload == {"ranges": [[1.0, 3.5]], "language": "de"}


def test_transcribe_range_takes_several_passages_in_one_job(client, file_row):
    """One job, one loaded model — and the passages sorted as they lie."""
    response = client.post(
        f"/api/files/{file_row['id']}/transcribe-range",
        json={
            "ranges": [
                {"start_s": 8.0, "end_s": 9.0},
                {"start_s": 1.0, "end_s": 2.0},
            ]
        },
    )
    assert response.status_code == 200
    assert json.loads(response.json()["payload"])["ranges"] == [[1.0, 2.0], [8.0, 9.0]]


def test_transcribe_range_merges_selections_that_overlap(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/transcribe-range",
        json={
            "ranges": [
                {"start_s": 1.0, "end_s": 4.0},
                {"start_s": 3.0, "end_s": 6.0},
            ]
        },
    )
    assert json.loads(response.json()["payload"])["ranges"] == [[1.0, 6.0]]


def test_transcribe_range_rejects_invalid_span(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/transcribe-range",
        json={"ranges": [{"start_s": 3.0, "end_s": 1.0}]},
    )
    assert response.status_code == 422


def test_transcribe_range_rejects_an_empty_list(client, file_row):
    response = client.post(f"/api/files/{file_row['id']}/transcribe-range", json={"ranges": []})
    assert response.status_code == 422


# ── cutting the recording ─────────────────────────────────────────────


def test_apply_cuts_enqueues_the_spans_that_stay(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/audio/apply",
        json={"keeps": [{"start_s": 0.0, "end_s": 2.0}, {"start_s": 5.0, "end_s": 9.0}]},
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "audio_edit"
    assert json.loads(response.json()["payload"])["keeps"] == [[0.0, 2.0], [5.0, 9.0]]


def test_apply_cuts_refuses_a_recording_that_is_not_cut_at_all(client, file_row):
    """Keeping everything is not an edit — re-encoding it would only cost quality."""
    with db.get_conn() as conn:  # the fixture's stub file has no readable duration
        conn.execute("UPDATE files SET duration = 30 WHERE id = ?", (file_row["id"],))
    response = client.post(
        f"/api/files/{file_row['id']}/audio/apply",
        json={"keeps": [{"start_s": 0.0, "end_s": 30.0}]},
    )
    assert response.status_code == 422


def test_apply_cuts_refuses_an_empty_result(client, file_row):
    response = client.post(f"/api/files/{file_row['id']}/audio/apply", json={"keeps": []})
    assert response.status_code == 422


def test_an_untouched_recording_cannot_be_restored(client, file_row):
    assert client.get(f"/api/files/{file_row['id']}/audio/original").json() == {
        "can_restore": False
    }
    assert client.post(f"/api/files/{file_row['id']}/audio/restore").status_code == 404


# ── ffmpeg command construction (pure) ────────────────────────────────


def test_one_span_becomes_a_plain_trim():
    """No filter graph: ffmpeg may seek to the start instead of decoding to it."""
    cmd = build_keep_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), [(1.0, 5.0)])
    assert cmd[:6] == ["ffmpeg", "-y", "-ss", "1.000", "-to", "5.000"]
    assert "-filter_complex" not in cmd


def test_several_spans_are_concatenated_in_one_pass():
    cmd = build_keep_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), [(0.0, 5.0), (8.0, 20.0)])
    joined = " ".join(cmd)
    assert "atrim=start=0.000:end=5.000" in joined
    assert "atrim=start=8.000:end=20.000" in joined
    assert "concat=n=2" in joined
    assert cmd[-1] == "out.mp3"


def test_cutting_a_recording_down_to_nothing_is_refused():
    with pytest.raises(ValueError):
        build_keep_command("ffmpeg", Path("in.mp3"), Path("out.mp3"), [])


def test_update_file_language_endpoint(client, file_row):
    response = client.put(f"/api/files/{file_row['id']}/language", json={"language": "RU"})
    assert response.status_code == 200
    assert response.json()["language"] == "ru"  # normalised, so whisper can use it verbatim
    saved = client.get(f"/api/files/{file_row['id']}/segments").json()
    assert saved["file"]["language"] == "ru"


def test_update_file_language_back_to_automatic(client, file_row):
    client.put(f"/api/files/{file_row['id']}/language", json={"language": "ru"})
    response = client.put(f"/api/files/{file_row['id']}/language", json={"language": ""})
    assert response.status_code == 200
    assert response.json()["language"] == ""


def test_update_file_language_rejects_unknown_code(client, file_row):
    response = client.put(f"/api/files/{file_row['id']}/language", json={"language": "klingon"})
    assert response.status_code == 422
