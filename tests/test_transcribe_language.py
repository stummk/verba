"""Which language a file is transcribed in, and what a selection gives back.

Two things the editor depends on:

- The language is *stated*, not guessed, whenever anything states it. Whisper's
  detector hears the first seconds and does get them wrong — a Russian
  recording read as German is transcribed, cleaned up and "translated" in the
  wrong language, and nothing downstream can notice.
- Transcribing a selection hands back text and writes no segments; only the
  `transcribe` job over the whole file rebuilds them.

The same holds when an OpenAI-compatible endpoint recognises the speech
instead of faster-whisper; how its answers become segments is tested at the end.
"""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import audio, transcripts, whisper, workspace

NO_CANCEL = threading.Event()


def noop_report(percent: int, message: str = "") -> None:
    pass


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start, self.end, self.text = start, end, text
        self.words: list[object] = []  # only filled where the speakers are recognised


class FakeInfo:
    def __init__(self, language: str, duration: float) -> None:
        self.language, self.duration = language, duration


class FakeModel:
    """Records the language it was asked for and always "hears" German."""

    def __init__(self, segments: list[FakeSegment]) -> None:
        self.segments = segments
        self.asked_for: object = "not called"
        self.word_timestamps: object = "not called"

    def transcribe(self, path, language=None, beam_size=5, word_timestamps=False, **kwargs):
        # **kwargs: the real signature keeps growing (hotwords, ...), and a
        # double that refuses one of them fails for the wrong reason
        self.asked_for = language
        self.word_timestamps = word_timestamps
        return iter(self.segments), FakeInfo("de", 6.0)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.whisper.language = ""
    config.save_settings(settings)
    db.init_db()
    # the follow-up jobs a finished transcription enqueues must not run here
    for kind in ("index_file", "llm_process"):
        monkeypatch.setitem(job_queue._handlers, kind, lambda job, cancel, report: None)
    return tmp_path


@pytest.fixture()
def file_row(env):
    project = workspace.create_project("Predigten")
    source = env / "20260731_ru_de_Wesner.mp3"
    source.write_bytes(b"x")
    [row] = workspace.import_paths(project, [str(source)])
    return workspace.get_file(row["id"])


def install_model(monkeypatch, segments: list[FakeSegment]) -> FakeModel:
    model = FakeModel(segments)
    monkeypatch.setattr(whisper, "get_model", lambda model_override="": model)
    monkeypatch.setattr(whisper, "_publish_engine_status", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "_publish_engine_status_running", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "probe_duration", lambda path: 6.0)
    return model


# ── which language wins ───────────────────────────────────────────────


def test_the_file_name_decides_over_the_detector(file_row):
    """`20260731_ru_de_…` states Russian — that is what is transcribed."""
    assert file_row["language"] == "ru"
    assert whisper.language_for(file_row) == "ru"


def test_a_run_option_outranks_the_file(file_row):
    assert whisper.language_for(file_row, {"language": "uk"}) == "uk"


def test_the_setting_fills_in_for_a_file_that_says_nothing(env):
    settings = config.get_settings()
    settings.whisper.language = "en"
    config.save_settings(settings)
    assert whisper.language_for({"language": ""}) == "en"


def test_nothing_stated_means_automatic_detection(env):
    assert whisper.language_for({"language": ""}) == ""


# ── the whole file ────────────────────────────────────────────────────


def test_transcription_asks_for_the_declared_language(file_row, monkeypatch):
    model = install_model(monkeypatch, [FakeSegment(0.0, 2.0, "Привет")])
    job = {"file_id": file_row["id"], "payload": {}}

    whisper.handle_transcribe_job(job, NO_CANCEL, noop_report)

    assert model.asked_for == "ru"


def test_a_declared_language_survives_the_detector(file_row, monkeypatch):
    """The model reports German; the file stays Russian, as its name says."""
    install_model(monkeypatch, [FakeSegment(0.0, 2.0, "Привет")])

    whisper.handle_transcribe_job(
        {"file_id": file_row["id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert workspace.get_file(file_row["id"])["language"] == "ru"


def test_detection_still_fills_a_file_that_declares_nothing(file_row, monkeypatch):
    workspace.update_file(file_row["id"], {"language": ""})
    install_model(monkeypatch, [FakeSegment(0.0, 2.0, "Guten Tag")])

    whisper.handle_transcribe_job(
        {"file_id": file_row["id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert workspace.get_file(file_row["id"])["language"] == "de"


def test_transcribing_the_file_again_rebuilds_the_segments(file_row, monkeypatch):
    install_model(monkeypatch, [FakeSegment(0.0, 2.0, "eins"), FakeSegment(2.0, 4.0, "zwei")])
    job = {"file_id": file_row["id"], "payload": {}}

    whisper.handle_transcribe_job(job, NO_CANCEL, noop_report)
    whisper.handle_transcribe_job(job, NO_CANCEL, noop_report)  # a second run replaces, not appends

    texts = [s["text"] for s in transcripts.list_segments(file_row["id"])]
    assert texts == ["eins", "zwei"]


# ── the selection ─────────────────────────────────────────────────────


@pytest.fixture()
def range_job(file_row, monkeypatch):
    monkeypatch.setattr(
        audio, "extract_range", lambda source, start_s, end_s, target: Path(target).write_bytes(b"")
    )
    return {
        "file_id": file_row["id"],
        "payload": {"ranges": [[1.0, 3.0]]},
    }


def test_a_transcribed_selection_writes_no_segments(range_job, monkeypatch):
    install_model(
        monkeypatch, [FakeSegment(0.0, 1.0, "Das ist"), FakeSegment(1.0, 2.0, "der Text")]
    )

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    assert transcripts.list_segments(range_job["file_id"]) == []


def test_a_transcribed_selection_publishes_its_text(range_job, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        whisper.hub,
        "publish",
        lambda event_type, data=None, **scope: events.append({"type": event_type, "data": data}),
    )
    install_model(
        monkeypatch, [FakeSegment(0.0, 1.0, "Das ist"), FakeSegment(1.0, 2.0, "der Text")]
    )

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    [event] = [e for e in events if e["type"] == "range.text"]
    assert event["data"]["text"] == "Das ist der Text"
    assert event["data"]["file_id"] == range_job["file_id"]
    assert (event["data"]["start_s"], event["data"]["end_s"]) == (1.0, 3.0)


def test_a_selection_is_transcribed_in_the_declared_language(range_job, monkeypatch):
    model = install_model(monkeypatch, [FakeSegment(0.0, 1.0, "Привет")])

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    assert model.asked_for == "ru"


def test_several_selections_are_transcribed_in_one_job(range_job, monkeypatch):
    """One job, one loaded model, one event per passage — the editor lists them."""
    range_job["payload"]["ranges"] = [[1.0, 3.0], [5.0, 6.0]]
    events: list[dict] = []
    monkeypatch.setattr(
        whisper.hub,
        "publish",
        lambda event_type, data=None, **scope: events.append({"type": event_type, "data": data}),
    )
    install_model(monkeypatch, [FakeSegment(0.0, 1.0, "Text")])

    whisper.handle_transcribe_range_job(range_job, NO_CANCEL, noop_report)

    published = [e["data"] for e in events if e["type"] == "range.text"]
    assert [(p["start_s"], p["end_s"]) for p in published] == [(1.0, 3.0), (5.0, 6.0)]
    assert [p["index"] for p in published] == [0, 1]
    assert {p["total"] for p in published} == {2}


def test_a_silent_selection_says_so_in_german(range_job, monkeypatch):
    install_model(monkeypatch, [])
    messages: list[str] = []

    whisper.handle_transcribe_range_job(
        range_job, NO_CANCEL, lambda percent, message="": messages.append(message)
    )

    assert "kein Text erkannt" in messages[-1]


# ── an OpenAI-compatible endpoint ─────────────────────────────────────


@pytest.fixture()
def endpoint(env, monkeypatch):
    """The endpoint engine against a fake server; `requests` records each upload."""
    settings = config.get_settings()
    settings.whisper.engine = "openai"
    settings.whisper.base_url = "http://asr.test/v1/"
    settings.whisper.api_key = "sk-test"
    config.save_settings(settings)
    whisper._endpoint_unsupported.clear()
    monkeypatch.setattr(whisper, "_publish_engine_status", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "_publish_engine_status_running", lambda *a, **k: None)
    monkeypatch.setattr(whisper, "probe_duration", lambda path: 6.0)
    pieces: list[tuple[float, float]] = []

    def fake_extract(source, start_s, end_s, target):
        pieces.append((start_s, end_s))
        Path(target).write_bytes(b"fLaC")

    monkeypatch.setattr(audio, "extract_range", fake_extract)
    state: dict = {"requests": [], "answers": [], "pieces": pieces}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", errors="replace")
        state["requests"].append(
            {"url": str(request.url), "headers": request.headers, "body": body}
        )
        answer = state["answers"].pop(0)
        return answer(body) if callable(answer) else httpx.Response(200, json=answer)

    real_client = httpx.Client

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(whisper.httpx, "Client", client)
    return state


def recognise(**kwargs):
    segments, info = whisper._recognise(Path("aufnahme.mp3"), cancel=NO_CANCEL, **kwargs)
    return list(segments), info


def form_value(body: str, name: str) -> str:
    """The value of one multipart field of a recorded upload."""
    marker = f'name="{name}"\r\n\r\n'
    assert marker in body, name
    return body.split(marker, 1)[1].split("\r\n", 1)[0]


def test_the_endpoint_is_asked_with_the_declared_language(file_row, endpoint):
    endpoint["answers"].append(
        {"language": "russian", "segments": [{"start": 0.0, "end": 2.0, "text": " Привет"}]}
    )

    whisper.handle_transcribe_job(
        {"file_id": file_row["id"], "payload": {}}, NO_CANCEL, noop_report
    )

    [request] = endpoint["requests"]
    assert request["url"] == "http://asr.test/v1/audio/transcriptions"
    assert request["headers"]["authorization"] == "Bearer sk-test"
    assert form_value(request["body"], "language") == "ru"
    assert form_value(request["body"], "response_format") == "verbose_json"
    assert form_value(request["body"], "model") == "whisper-1"
    [segment] = transcripts.list_segments(file_row["id"])
    assert segment["text"] == "Привет"
    assert workspace.get_file(file_row["id"])["status"] == "done"


def test_the_endpoint_detection_fills_a_file_that_declares_nothing(file_row, endpoint):
    workspace.update_file(file_row["id"], {"language": ""})
    endpoint["answers"].append(
        {"language": "german", "segments": [{"start": 0.0, "end": 2.0, "text": "Guten Tag"}]}
    )

    whisper.handle_transcribe_job(
        {"file_id": file_row["id"], "payload": {}}, NO_CANCEL, noop_report
    )

    assert workspace.get_file(file_row["id"])["language"] == "de"


def test_a_long_recording_is_sent_in_pieces_cut_at_pauses(endpoint, monkeypatch):
    monkeypatch.setattr(whisper, "probe_duration", lambda path: 1300.0)
    monkeypatch.setattr(audio, "scan_pauses", lambda path: (1300.0, [(589.0, 593.0)]))
    for text in ("eins", "zwei", "drei"):
        endpoint["answers"].append({"segments": [{"start": 1.0, "end": 2.0, "text": text}]})

    segments, info = recognise()

    assert endpoint["pieces"] == [(0.0, 591.0), (591.0, 1191.0), (1191.0, 1300.0)]
    assert [(s["start"], s["text"]) for s in segments] == [
        (1.0, "eins"),
        (592.0, "zwei"),
        (1192.0, "drei"),
    ]
    assert info.duration == 1300.0


def test_plain_words_get_their_spaces_and_their_segment(endpoint):
    """OpenAI lists the words once for the whole answer and without spaces."""
    endpoint["answers"].append(
        {
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "Guten Tag"},
                {"start": 1.0, "end": 2.0, "text": "liebe Leute"},
            ],
            "words": [
                {"word": "Guten", "start": 0.0, "end": 0.4},
                {"word": "Tag", "start": 0.5, "end": 0.9},
                {"word": "liebe", "start": 1.0, "end": 1.4},
                {"word": "Leute", "start": 1.5, "end": 1.9},
            ],
        }
    )

    segments, _info = recognise(words=True)

    assert [w[2] for w in segments[0]["words"]] == ["Guten", " Tag"]
    assert [w[2] for w in segments[1]["words"]] == ["liebe", " Leute"]
    assert "".join(w[2] for w in segments[1]["words"]) == segments[1]["text"]


def test_tokens_keep_their_own_spaces(endpoint):
    """whisper.cpp hands out tokens per segment, special tokens included."""
    endpoint["answers"].append(
        {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": " Hallo Welt",
                    "words": [
                        {"word": "[_BEG_]", "start": 0.0, "end": 0.0},
                        {"word": " Hal", "start": 0.0, "end": 0.2},
                        {"word": "lo", "start": 0.2, "end": 0.4},
                        {"word": " Welt", "start": 0.5, "end": 0.9},
                    ],
                }
            ]
        }
    )

    [segment], _info = recognise(words=True)

    assert "".join(w[2] for w in segment["words"]) == " Hallo Welt"


def test_a_refused_field_is_dropped_for_that_endpoint(endpoint):
    endpoint["answers"].append(
        lambda body: httpx.Response(400, json={"error": "unknown parameter: token_timestamps"})
    )
    endpoint["answers"].append({"segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]})
    endpoint["answers"].append({"segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]})

    recognise(words=True)
    recognise(words=True)  # the next upload does not ask again

    first, retry, later = endpoint["requests"]
    assert 'name="token_timestamps"' in first["body"]
    assert 'name="token_timestamps"' not in retry["body"]
    assert 'name="token_timestamps"' not in later["body"]
    assert 'name="timestamp_granularities[]"' in retry["body"]


def test_an_answer_without_segments_is_one_segment_for_the_piece(endpoint):
    endpoint["answers"].append({"text": " Alles in einem Stück "})

    [segment], _info = recognise()

    assert (segment["start"], segment["end"]) == (0.0, 6.0)
    assert segment["text"] == "Alles in einem Stück"


def test_an_endpoint_error_fails_the_file_in_german(file_row, endpoint):
    endpoint["answers"].append(lambda body: httpx.Response(500, text="model not loaded"))

    with pytest.raises(RuntimeError, match="Transkriptions-Endpunkt antwortete mit 500"):
        whisper.handle_transcribe_job(
            {"file_id": file_row["id"], "payload": {}}, NO_CANCEL, noop_report
        )

    row = workspace.get_file(file_row["id"])
    assert row["status"] == "failed"
    assert "model not loaded" in row["error"]


def test_an_endpoint_without_url_says_where_to_set_it(endpoint):
    settings = config.get_settings()
    settings.whisper.base_url = ""
    config.save_settings(settings)

    with pytest.raises(RuntimeError, match="Kein Transkriptions-Endpunkt"):
        recognise()


def test_a_cancelled_transcription_returns_the_file_to_pending(file_row, endpoint):
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(whisper.JobCancelled):
        whisper.handle_transcribe_job(
            {"file_id": file_row["id"], "payload": {}}, cancel, noop_report
        )

    assert endpoint["requests"] == []
    assert workspace.get_file(file_row["id"])["status"] == "pending"


def test_chunks_cut_at_the_limit_where_there_is_no_pause():
    assert whisper.plan_chunks(1250.0, [], limit=600.0) == [
        (0.0, 600.0),
        (600.0, 1200.0),
        (1200.0, 1250.0),
    ]
    assert whisper.plan_chunks(300.0, [], limit=600.0) == [(0.0, 300.0)]


def test_an_unknown_duration_is_one_piece_to_the_end():
    [(start, end)] = whisper.plan_chunks(None, [])
    assert start == 0.0 and end > 86400


def test_a_long_list_of_spellings_is_shortened_to_whole_terms(endpoint):
    terms = [f"Begriff{index:03d}" for index in range(100)]
    endpoint["answers"].append({"segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]})

    recognise(hotwords=", ".join(terms))

    prompt = form_value(endpoint["requests"][0]["body"], "prompt")
    assert len(prompt) <= whisper.ENDPOINT_PROMPT_CHARS
    sent = prompt.split(", ")
    assert sent == terms[: len(sent)]  # the best attested first, none cut in half


def test_a_short_list_of_spellings_is_sent_unchanged():
    assert whisper.fit_prompt("Wesner, Kontra") == "Wesner, Kontra"
    assert whisper.fit_prompt("") == ""
    assert whisper.fit_prompt("x" * 400) == ""  # a single term too long for the limit
