"""The two ways in, and the order the steps run in.

The recognition is reachable twice: the transcript type has it run by itself
after a transcription, and the editor starts it for a single recording. Both
have to refuse politely when the component is not installed — and the
automatic one has to run *before* the search index and the LLM steps, because
it rewrites the very segments those read.
"""

from __future__ import annotations

import threading
import time

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import diarize, project_types, transcripts, whisper, workspace

NO_CANCEL = threading.Event()


def noop_report(percent: int, message: str = "") -> None:
    pass


@pytest.fixture(autouse=True)
def workspaces_in_tmp(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.general.browse_roots = [str(tmp_path)]
    config.save_settings(settings)


@pytest.fixture()
def installed(monkeypatch):
    """The component and both models, without any of them on disk."""
    monkeypatch.setattr(diarize, "library_available", lambda: True)
    monkeypatch.setattr(diarize, "ready", lambda: True)


@pytest.fixture()
def file_row(client, tmp_path, monkeypatch):
    for kind in ("transcribe", "diarize", "index_file", "llm_process"):
        monkeypatch.setitem(job_queue._handlers, kind, lambda job, cancel, report: None)
    (tmp_path / "gespraech.mp3").write_bytes(b"x")
    project = client.post("/api/projects", json={"name": "Interviews"}).json()
    [row] = client.post(
        f"/api/projects/{project['id']}/files/import",
        json={"paths": [str(tmp_path / "gespraech.mp3")]},
    ).json()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) "
            "VALUES (?, 0, 0, 2, 'Guten Tag.')",
            (row["id"],),
        )
    return row


# ── starting it by hand ───────────────────────────────────────────────


def test_the_editor_can_start_a_recognition(client, file_row, installed):
    """No body: the run has nothing to say about how many voices to look for."""
    response = client.post(f"/api/files/{file_row['id']}/diarize")

    assert response.status_code == 202
    job = response.json()
    assert job["kind"] == "diarize"
    assert job["file_id"] == file_row["id"]
    assert job_queue.get(job["id"])["payload"] == "{}"


def test_a_missing_component_is_refused_in_german(client, file_row, monkeypatch):
    monkeypatch.setattr(diarize, "library_available", lambda: False)

    response = client.post(f"/api/files/{file_row['id']}/diarize", json={})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "nicht installiert" in detail
    # and it says where to install it — "somewhere in the settings" is what
    # sent the first user of this looking in the wrong section
    assert "Sprechererkennung" in detail


def test_missing_models_are_refused_too(client, file_row, monkeypatch):
    monkeypatch.setattr(diarize, "library_available", lambda: True)
    monkeypatch.setattr(diarize, "ready", lambda: False)

    response = client.post(f"/api/files/{file_row['id']}/diarize", json={})

    assert response.status_code == 409
    assert "Modelle" in response.json()["detail"]


def test_a_recording_without_a_transcript_is_refused(client, file_row, installed):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM segments WHERE file_id = ?", (file_row["id"],))

    response = client.post(f"/api/files/{file_row['id']}/diarize", json={})

    assert response.status_code == 409
    assert "transkribiert" in response.json()["detail"]


def test_a_second_run_for_the_same_recording_is_refused(client, file_row, installed, monkeypatch):
    """A recognition that is already under way is not started again.

    The first job is held inside its handler for the length of the test, so
    "already running" is a fact here rather than a race the queue might win.
    """
    started, release = threading.Event(), threading.Event()

    def blocking(job, cancel, report):
        started.set()
        release.wait(10)

    monkeypatch.setitem(job_queue._handlers, "diarize", blocking)
    try:
        assert client.post(f"/api/files/{file_row['id']}/diarize").status_code == 202
        assert started.wait(10), "the queue never picked the job up"

        response = client.post(f"/api/files/{file_row['id']}/diarize")
    finally:
        release.set()

    assert response.status_code == 409


# ── the names ─────────────────────────────────────────────────────────


def test_no_route_states_the_number_of_speakers(client, file_row):
    """There is nothing to state it with; the count comes from the audio."""
    response = client.put(f"/api/files/{file_row['id']}/speaker-count", json={"speaker_count": 3})

    assert response.status_code == 405


def test_renaming_a_speaker_reaches_every_segment(client, file_row):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE segments SET speaker = 'Sprecher 1' WHERE file_id = ?", (file_row["id"],)
        )
        conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker) "
            "VALUES (?, 1, 2, 4, 'Und weiter.', 'Sprecher 1')",
            (file_row["id"],),
        )

    response = client.post(
        f"/api/files/{file_row['id']}/speakers/rename",
        json={"old_name": "Sprecher 1", "new_name": "Frau Berger"},
    )

    assert response.status_code == 200
    assert response.json()["renamed"] == 2
    speakers = {row["speaker"] for row in transcripts.list_segments(file_row["id"])}
    assert speakers == {"Frau Berger"}


def test_renaming_a_speaker_nobody_is_called_changes_nothing(client, file_row):
    response = client.post(
        f"/api/files/{file_row['id']}/speakers/rename",
        json={"old_name": "Sprecher 9", "new_name": "X"},
    )

    assert response.json()["renamed"] == 0


# ── installing the component from where it is missed ──────────────────


def test_the_component_can_be_installed_from_its_own_section(client, monkeypatch):
    """The models are downloadable without the library, so the two can drift.

    Whoever downloads 100 MB of models next to a component that is not there
    must be able to install it right there, not by walking the first-run
    wizard again.
    """
    from verba import setup_check

    started: list[str] = []
    monkeypatch.setattr(setup_check, "run_feature_group", started.append)
    monkeypatch.setattr(setup_check, "group_installed", lambda group: False)

    response = client.post("/api/system/component/diarize")

    assert response.status_code == 200
    assert response.json()["started"] is True
    for _ in range(50):  # the runner is a thread
        if started:
            break
        time.sleep(0.02)
    assert started == ["diarize"]


def test_an_unknown_component_is_a_404(client):
    assert client.post("/api/system/component/nonsense").status_code == 404


def test_a_component_that_is_there_is_not_installed_again(client, monkeypatch):
    from verba import setup_check

    monkeypatch.setattr(setup_check, "group_installed", lambda group: True)

    response = client.post("/api/system/component/diarize")

    assert response.json()["started"] is False
    assert "installiert" in response.json()["reason"]


def test_the_status_names_the_packages_it_cannot_find(monkeypatch):
    """ "Not installed" is a dead end; "sherpa_onnx is missing" is a lead."""
    from verba import setup_check

    monkeypatch.setattr(setup_check, "_module_installed", lambda name: name != "sherpa_onnx")

    assert diarize.missing_modules() == ["sherpa_onnx"]
    assert diarize.status()["missing"] == ["sherpa_onnx"]
    assert diarize.status()["available"] is False


# ── the transcript type decides, and the order is fixed ───────────────


class FakeSegment:
    def __init__(self, start: float, end: float, text: str, words: list | None = None) -> None:
        self.start, self.end, self.text = start, end, text
        self.words = words or []


class FakeWord:
    def __init__(self, start: float, end: float, word: str) -> None:
        self.start, self.end, self.word = start, end, word


class FakeInfo:
    language = "de"
    duration = 6.0


class FakeModel:
    def __init__(self, segments: list[FakeSegment]) -> None:
        self.segments = segments
        self.word_timestamps: object = "not called"

    def transcribe(self, path, language=None, beam_size=5, word_timestamps=False):
        self.word_timestamps = word_timestamps
        return iter(self.segments), FakeInfo()


@pytest.fixture()
def transcription(tmp_path, monkeypatch):
    """A file whose transcription can be run, and the queue watching it."""
    db.init_db()
    project_types.seed_builtin_types()
    for kind in ("diarize", "index_file", "llm_process"):
        monkeypatch.setitem(job_queue._handlers, kind, lambda job, cancel, report: None)
    monkeypatch.setattr(whisper, "_publish_engine_status", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "_publish_engine_status_running", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "probe_duration", lambda path: 6.0)

    interview = project_types.get_type_by_key("interview")
    song = project_types.get_type_by_key("song")
    project = workspace.create_project("Interviews", type_id=interview["id"])
    source = tmp_path / "gespraech.mp3"
    source.write_bytes(b"x")
    [row] = workspace.import_paths(project, [str(source)])

    model = FakeModel(
        [
            FakeSegment(
                0.0,
                4.0,
                "Wie geht es Ihnen? Danke, gut.",
                [FakeWord(0.0, 2.0, " Wie geht es Ihnen?"), FakeWord(2.5, 4.0, " Danke, gut.")],
            )
        ]
    )
    monkeypatch.setattr(whisper, "get_model", lambda model_override="": model)
    return {"file_id": row["id"], "model": model, "song_type": song["id"], "project": project}


def kinds_queued(file_id: int) -> list[str]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT kind FROM jobs WHERE file_id = ? ORDER BY id", (file_id,)
        ).fetchall()
    return [row["kind"] for row in rows]


def test_a_conversation_type_has_its_words_timed(transcription, monkeypatch):
    """The word timings are what a segment is later cut at, and cost time.

    So they are recorded exactly where they will be needed, and nowhere else.
    """
    monkeypatch.setattr(diarize, "ready", lambda: True)

    whisper.handle_transcribe_job(
        {"file_id": transcription["file_id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert transcription["model"].word_timestamps is True
    stored = transcripts.list_segments(transcription["file_id"])[0]
    assert transcripts.decode_words(stored["words"])[0][2] == " Wie geht es Ihnen?"


def test_a_single_voice_type_does_not_pay_for_them(transcription, monkeypatch):
    workspace.update_file(transcription["file_id"], {"type_id": transcription["song_type"]})
    monkeypatch.setattr(diarize, "ready", lambda: True)

    whisper.handle_transcribe_job(
        {"file_id": transcription["file_id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert transcription["model"].word_timestamps is False
    assert transcripts.list_segments(transcription["file_id"])[0]["words"] == ""


@pytest.fixture()
def followups(monkeypatch):
    """Who was asked to run next.

    Whether the search index and the LLM steps actually enqueue anything
    depends on the machine (an embedding model installed, an endpoint
    configured); what this is about is *who is asked*, and when.
    """
    from verba.services import pipeline, vectorstore

    called: list[str] = []
    monkeypatch.setattr(vectorstore, "maybe_enqueue_index", lambda *a, **k: called.append("index"))
    monkeypatch.setattr(
        pipeline, "maybe_enqueue_auto_process", lambda *a, **k: called.append("llm")
    )
    return called


def test_the_recognition_runs_before_the_index_and_the_llm(transcription, followups, monkeypatch):
    """Both of them read the segments the recognition is about to rewrite."""
    monkeypatch.setattr(diarize, "ready", lambda: True)

    whisper.handle_transcribe_job(
        {"file_id": transcription["file_id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert kinds_queued(transcription["file_id"]) == ["diarize"]
    assert followups == []  # they wait for it


def test_without_the_recognition_they_run_straight_away(transcription, followups, monkeypatch):
    workspace.update_file(transcription["file_id"], {"type_id": transcription["song_type"]})

    whisper.handle_transcribe_job(
        {"file_id": transcription["file_id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert followups == ["index", "llm"]
    assert "diarize" not in kinds_queued(transcription["file_id"])


def test_a_type_that_wants_speakers_without_the_component_says_so(transcription, monkeypatch):
    """The transcription still finishes — it just says what it could not do."""
    monkeypatch.setattr(diarize, "ready", lambda: False)
    messages: list[str] = []

    whisper.handle_transcribe_job(
        {"file_id": transcription["file_id"], "payload": {}},
        NO_CANCEL,
        lambda percent, message="": messages.append(message),
    )

    assert "Sprechererkennung nicht eingerichtet" in messages[-1]
    assert "diarize" not in kinds_queued(transcription["file_id"])
    assert workspace.get_file(transcription["file_id"])["status"] == "done"


def test_the_chained_run_goes_on_to_the_llm_steps(transcription, followups, monkeypatch):
    """What the transcription handed over, the recognition hands on."""
    monkeypatch.setattr(diarize, "run", lambda *a, **k: [diarize.Turn(0.0, 4.0, 0)])
    file_id = transcription["file_id"]
    transcripts.replace_all_segments(
        file_id, [{"start_s": 0.0, "end_s": 4.0, "text": "Guten Tag.", "speaker": ""}]
    )

    diarize.handle_diarize_job(
        {"file_id": file_id, "payload": {"chain": True}, "session_id": ""}, NO_CANCEL, noop_report
    )

    assert followups == ["index", "llm"]


def test_a_manual_run_only_reindexes(transcription, followups, monkeypatch):
    """Clicking "recognise speakers" is not a request to process the file.

    The search index does have to be told — the segments it holds have just
    been split — but the LLM steps are the user's own decision.
    """
    monkeypatch.setattr(diarize, "run", lambda *a, **k: [diarize.Turn(0.0, 4.0, 0)])
    file_id = transcription["file_id"]
    transcripts.replace_all_segments(
        file_id, [{"start_s": 0.0, "end_s": 4.0, "text": "Guten Tag.", "speaker": ""}]
    )

    diarize.handle_diarize_job(
        {"file_id": file_id, "payload": {}, "session_id": ""}, NO_CANCEL, noop_report
    )

    assert followups == ["index"]


def test_a_recording_with_no_recognisable_voice_still_hands_on(
    transcription, followups, monkeypatch
):
    """The steps were held back for this job — they must not be lost with it.

    Silence, music, a recording the segmentation heard nobody in: the
    transcript stays as it is, and the cleanup and the index still run.
    """
    monkeypatch.setattr(diarize, "run", lambda *a, **k: [])
    file_id = transcription["file_id"]
    transcripts.replace_all_segments(
        file_id, [{"start_s": 0.0, "end_s": 4.0, "text": "Guten Tag.", "speaker": "Anna"}]
    )
    messages: list[str] = []

    diarize.handle_diarize_job(
        {"file_id": file_id, "payload": {"chain": True}, "session_id": ""},
        NO_CANCEL,
        lambda percent, message="": messages.append(message),
    )

    assert followups == ["index", "llm"]
    assert "keine Sprecher erkannt" in messages[-1]
    # and the name somebody typed there is still theirs
    assert transcripts.list_segments(file_id)[0]["speaker"] == "Anna"


def test_the_progress_line_is_german_and_names_the_file(transcription, monkeypatch):
    monkeypatch.setattr(diarize, "run", lambda *a, **k: [diarize.Turn(0.0, 4.0, 0)])
    file_id = transcription["file_id"]
    transcripts.replace_all_segments(
        file_id, [{"start_s": 0.0, "end_s": 4.0, "text": "Guten Tag.", "speaker": ""}]
    )
    messages: list[str] = []

    diarize.handle_diarize_job(
        {"file_id": file_id, "payload": {}, "session_id": ""},
        NO_CANCEL,
        lambda percent, message="": messages.append(message),
    )

    assert "gespraech.mp3" in messages[0]
    assert "Sprecher" in messages[-1]
