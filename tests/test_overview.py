"""The whole transcript reaches the aufbereitung, without any call overrunning.

A recording is chunked because no context window holds two hours of speech.
What the document has only once — its title, its summary, its list of
decisions — must not be produced once per chunk all the same; that is what
turned a 20-minute recording into four protocols with four titles. So the
whole text is read once (map → fold), and everything document-level is
written from that reading in a single call (reduce).
"""

from __future__ import annotations

import json
import threading

import pytest

from verba import config, db
from verba.services import overview, pipeline, project_types, transcripts, workspace

NO_CANCEL = threading.Event()


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    project_types.seed_builtin_types()


def body(index: int, chars: int) -> str:
    """Segment text of roughly `chars` characters, in many distinct words.

    A real transcript is not one word repeated: the guard that catches a
    summarized section counts the *distinct* words that survived, and text
    like `"wort " * 700` carries two of them — too little to judge.
    """
    return " ".join(f"begriff{index}n{n}" for n in range(max(1, chars // 12)))


def make_file(tmp_path, name="besprechung.mp3", segments=20, chars=700, type_key=""):
    """A file whose transcript needs several chunks (default ~14k chars)."""
    source = tmp_path / name
    source.write_bytes(b"x")
    project = workspace.create_project("Aufbereitung")
    if type_key:
        chosen = next(t for t in project_types.list_types() if t["key"] == type_key)
        workspace.update_project(project["id"], {"type_id": chosen["id"]})
    [file_row] = workspace.import_paths(project, [str(source)])
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    file_row["id"],
                    i,
                    i * 30.0,
                    (i + 1) * 30.0,
                    f"Abschnitt {i} " + body(i, chars * 5),
                )
                for i in range(segments)
            ],
        )
    return file_row


class Recorder:
    """Fake LLM that answers every kind of call and remembers what it got."""

    KINDS = {
        "You condense one section": "digest",
        "You merge the digests": "fold",
        "You state what one recording": "overview",
        "You write one document": "document",
        "You clean up automatic": "cleanup",
        "You translate transcriptions": "translate",
    }

    def __init__(self, **answers: object) -> None:
        self.calls: list[tuple[str, str, str]] = []  # (kind, system, user)
        self.answers = answers

    def kind_of(self, system: str) -> str:
        for prefix, kind in self.KINDS.items():
            if system.startswith(prefix):
                return kind
        return "other"

    def __call__(self, messages, model_override="", **kwargs):
        system, user = messages[0]["content"], messages[-1]["content"]
        kind = self.kind_of(system)
        self.calls.append((kind, system, user))
        answer = self.answers.get(kind)
        if callable(answer):
            return answer(user)
        if answer is not None:
            return answer
        if kind == "overview":
            return json.dumps(
                {
                    "title": "Quartalsbesprechung Vertrieb",
                    "summary": "Es ging um Zahlen und Termine.",
                    "topics": ["Zahlen", "Termine"],
                    "terms": ["Wesner", "Q3"],
                }
            )
        if kind == "document":
            return "Protokoll\n\nBeschlüsse: eins"
        return user  # digest, fold, cleanup, translate: echo the material

    def of(self, kind: str) -> list[tuple[str, str, str]]:
        return [call for call in self.calls if call[0] == kind]


def run(file_id: int, **payload):
    job = {"payload": {"file_id": file_id, "steps": ["cleanup"], **payload}}
    pipeline.handle_llm_process_job(job, threading.Event(), lambda p, m="": None)


# ── one document for the whole recording ─────────────────────────────


def test_a_transforming_type_writes_one_document_not_one_per_chunk(tmp_path, monkeypatch):
    """The bug: a protocol prompt per chunk yields a protocol per chunk."""
    file_row = make_file(tmp_path, type_key="protocol")
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert len(llm.of("digest")) > 1  # the text was read section by section
    assert len(llm.of("document")) == 1  # and turned into one document, once
    assert not llm.of("cleanup")  # the type's instruction never ran per chunk
    text = pipeline.get_text(file_row["id"], "cleanup")["content"]
    assert text.count("Protokoll") == 1


def test_the_document_is_written_from_every_section(tmp_path, monkeypatch):
    """Four sections whose digests fit into one call: nothing is folded away."""
    file_row = make_file(tmp_path, segments=4, type_key="protocol")
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    material = llm.of("document")[0][2]
    assert not llm.of("fold")
    for index in range(4):  # every section's content arrived, condensed
        assert f"Abschnitt {index} " in material
    instruction = llm.of("document")[0][1]
    assert "one single document for all of it" in instruction
    assert "meeting minutes" in instruction  # the type's own prompt


def test_a_short_recording_reaches_the_document_whole(tmp_path, monkeypatch):
    """One chunk is the whole material — nothing of it may be condensed away."""
    file_row = make_file(tmp_path, segments=3, chars=100, type_key="protocol")
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert not llm.of("digest")  # nothing to condense
    material = llm.of("document")[0][2]
    for index in range(3):
        assert f"Abschnitt {index} " in material


def test_one_title_for_the_file_instead_of_one_per_chunk(tmp_path, monkeypatch):
    file_row = make_file(tmp_path, type_key="protocol")
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert len(llm.of("overview")) == 1
    updated = workspace.get_file(file_row["id"])
    assert updated["title"] == "Quartalsbesprechung Vertrieb"
    assert updated["header_left"] == "Quartalsbesprechung Vertrieb"  # what the PDF prints


@pytest.mark.parametrize("name", ["besprechung.mp3", "REC_0042.mp3", "20260304.mp3"])
def test_a_name_that_states_no_title_gets_one(tmp_path, monkeypatch, name):
    """A name that is only a name — or only a date — says nothing about the
    recording, so what was read out of it is better than the file name."""
    file_row = make_file(tmp_path, name=name)
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    updated = workspace.get_file(file_row["id"])
    assert updated["title"] == "Quartalsbesprechung Vertrieb"
    assert updated["header_left"] == "Quartalsbesprechung Vertrieb"


def test_a_title_is_cut_to_what_the_header_line_holds(tmp_path, monkeypatch):
    """It goes into the line the PDF prints with the date flush right."""
    file_row = make_file(tmp_path)
    llm = Recorder(
        overview=json.dumps({"title": "Sehr ausführlicher Titel " * 20, "terms": ["Wesner"]})
    )
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    title = workspace.get_file(file_row["id"])["title"]
    assert 0 < len(title) <= overview.TITLE_MAX_CHARS


def test_a_cleared_header_stays_cleared(tmp_path, monkeypatch):
    file_row = make_file(tmp_path)
    workspace.update_file(file_row["id"], {"header_left": ""})
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    updated = workspace.get_file(file_row["id"])
    assert updated["header_left"] == ""  # emptied on purpose
    assert updated["title"] == "Quartalsbesprechung Vertrieb"


@pytest.mark.parametrize(
    "name, stated",
    [
        ("20260304_de_en_Predigt über Römer 8_Teil 1.mp3", "Predigt über Römer 8"),
        ("2026-03-04 Jahresrückblick.mp3", "Jahresrückblick"),  # a date, then a title
    ],
)
def test_a_title_the_file_name_states_stands(tmp_path, monkeypatch, name, stated):
    """The naming scheme is how a user states a title — it is never replaced."""
    file_row = make_file(tmp_path, name=name)
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert workspace.get_file(file_row["id"])["title"] == stated


def test_an_edited_header_is_never_overwritten(tmp_path, monkeypatch):
    file_row = make_file(tmp_path)
    workspace.update_file(file_row["id"], {"header_left": "Von Hand gesetzt"})
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    updated = workspace.get_file(file_row["id"])
    assert updated["header_left"] == "Von Hand gesetzt"
    assert updated["title"] == "Quartalsbesprechung Vertrieb"  # the title itself was free


# ── a reproducing type keeps every chunk, and learns the names ───────


def test_a_verbatim_type_still_cleans_every_chunk_but_knows_the_whole(tmp_path, monkeypatch):
    file_row = make_file(tmp_path, type_key="speech")
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert len(llm.of("cleanup")) > 1  # the text comes back in full, chunk by chunk
    assert not llm.of("document")
    for _kind, system, _user in llm.of("cleanup"):
        assert "Quartalsbesprechung Vertrieb" in system  # the orientation travels along
        assert "Wesner" in system  # so the same name is spelled the same way
        assert "Never output it" in system


@pytest.mark.parametrize("type_key", ["song", "poem", "speech", "roleplay", "interview"])
def test_a_reproducing_type_is_never_given_a_summary(tmp_path, monkeypatch, type_key):
    """Song, poem, speech, roleplay, interview: nothing is condensed anywhere.

    Not even the orientation may contain a shorter version of the text — a
    model that is shown one would be invited to hand one back.
    """
    file_row = make_file(tmp_path, type_key=type_key)
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert not llm.of("document")  # the whole-recording call never happens
    for _kind, system, _user in llm.of("cleanup"):
        assert "Es ging um Zahlen und Termine." not in system  # the summary
        assert "Sections:" not in system  # the topic list
        assert "Wesner" in system  # the spellings, which is the point
    # every chunk went through, and each answer carries its own text
    text = pipeline.get_text(file_row["id"], "cleanup")["content"]
    for index in range(20):
        assert f"Abschnitt {index} " in text


def test_a_summarized_section_is_refused_instead_of_stored(tmp_path, monkeypatch):
    """The promise of a verbatim type does not rest on the prompt alone."""
    file_row = make_file(tmp_path, type_key="poem")
    llm = Recorder(cleanup="Das Gedicht handelt von Liebe und Verlust.")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    with pytest.raises(RuntimeError, match="zusammengefasst"):
        run(file_row["id"])

    assert pipeline.get_text(file_row["id"], "cleanup") is None
    # asked twice for the first section: once with the orientation, once
    # without it, before the step gives up
    assert len(llm.of("cleanup")) == 2


def test_a_section_that_comes_back_whole_the_second_time_is_kept(tmp_path, monkeypatch):
    file_row = make_file(tmp_path, segments=6, type_key="poem")
    seen: list[str] = []

    def cleanup(user: str) -> str:
        seen.append(user)
        if len(seen) == 1:  # the first answer shortens, the retry does not
            return "Kurz gesagt: es geht um viel."
        return user

    llm = Recorder(cleanup=cleanup)
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    text = pipeline.get_text(file_row["id"], "cleanup")["content"]
    for index in range(6):
        assert f"Abschnitt {index} " in text


def test_a_transforming_type_may_condense_as_much_as_it_likes(tmp_path, monkeypatch):
    """The same guard must not fire where condensing is the whole job."""
    file_row = make_file(tmp_path, type_key="protocol")
    llm = Recorder(document="Protokoll\n\nKurz: es wurde entschieden.")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert pipeline.get_text(file_row["id"], "cleanup")["content"].startswith("Protokoll")


def test_the_translation_gets_the_same_glossary(tmp_path, monkeypatch):
    file_row = make_file(tmp_path)
    llm = Recorder(translate=lambda user: f"[EN] {user}")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"], steps=["translate"], target_language="en")

    for _kind, system, _user in llm.of("translate"):
        assert "Wesner" in system


def test_a_short_transcript_is_not_read_twice(tmp_path, monkeypatch):
    """One chunk holds everything: the cleanup call already sees the whole
    recording, so nothing is condensed — only the title is asked for."""
    file_row = make_file(tmp_path, segments=2, chars=20)
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert not llm.of("digest")
    assert not llm.of("fold")
    assert len(llm.of("overview")) == 1
    assert len(llm.of("cleanup")) == 1


# ── nothing overruns the context window ──────────────────────────────


def test_no_call_is_given_more_than_one_chunk_worth(tmp_path, monkeypatch):
    file_row = make_file(tmp_path, segments=60, type_key="protocol")
    # a model that pays no attention to the character budget it was given
    llm = Recorder(digest=lambda user: "sehr ausführlich " * 600)
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert max(len(user) for _k, _s, user in llm.calls) <= overview.FOLD_BUDGET
    assert llm.of("fold")  # it took folding to get there


def test_a_digest_that_ignores_its_budget_is_cut_back(monkeypatch):
    llm = Recorder(digest=lambda user: "wort " * 5000)
    monkeypatch.setattr(overview.llm, "chat", llm)
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    chunks = overview.chunking.chunk_segments(
        [{"text": "satz " * 200, "start_s": 0.0, "end_s": 10.0}]
    )

    digests = overview.section_digests(chunks, "", "", NO_CANCEL, lambda p, m="": None, (0, 100))

    assert len(digests[0]) <= overview.DIGEST_MAX_CHARS + 40  # plus the section label


def test_the_fold_terminates_even_when_nothing_gets_shorter(monkeypatch):
    """A model that answers a fold with the same length must not spin."""
    llm = Recorder(fold=lambda user: user)
    monkeypatch.setattr(overview.llm, "chat", llm)

    folded = overview.fold([f"Abschnitt {i} " + "wort " * 400 for i in range(40)], "", NO_CANCEL)

    assert len(folded) <= overview.FOLD_BUDGET


def test_a_cut_off_document_is_asked_again_with_less(tmp_path, monkeypatch):
    file_row = make_file(tmp_path, type_key="protocol")
    attempts: list[int] = []

    def document(user: str) -> str:
        attempts.append(len(user))
        if len(attempts) == 1:
            raise pipeline.llm.TruncatedAnswer("halbes Protokoll")
        return "Protokoll\n\nBeschlüsse: eins"

    llm = Recorder(document=document)
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert len(attempts) == 2
    assert attempts[1] < attempts[0]  # folded tighter instead of split in two
    assert "Protokoll" in pipeline.get_text(file_row["id"], "cleanup")["content"]


def test_a_document_that_stays_cut_off_fails_loudly(tmp_path, monkeypatch):
    file_row = make_file(tmp_path, type_key="protocol")

    def document(user: str) -> str:
        raise pipeline.llm.TruncatedAnswer("halbes Protokoll")

    llm = Recorder(document=document)
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    with pytest.raises(RuntimeError, match="nicht in einem Stück"):
        run(file_row["id"])
    assert pipeline.get_text(file_row["id"], "cleanup") is None


def test_an_unusable_overview_answer_does_not_fail_the_step(tmp_path, monkeypatch):
    """The overview is orientation, not content — a step works without it."""
    file_row = make_file(tmp_path)
    llm = Recorder(overview="Ich kann dazu nichts sagen.")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])

    assert pipeline.get_text(file_row["id"], "cleanup")["content"]
    assert workspace.get_file(file_row["id"])["title"] == "besprechung"  # from the file name


# ── read once, kept until the transcript changes ─────────────────────


def test_the_reading_is_kept_for_the_next_step(tmp_path, monkeypatch):
    file_row = make_file(tmp_path)
    llm = Recorder(translate=lambda user: f"[EN] {user}")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"], steps=["cleanup", "translate"], target_language="en")

    assert len(llm.of("overview")) == 1  # not once per step
    stored = overview.load(file_row["id"])
    assert stored is not None
    assert stored.title == "Quartalsbesprechung Vertrieb"


def test_an_edited_transcript_is_read_again(tmp_path, monkeypatch):
    file_row = make_file(tmp_path)
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)
    run(file_row["id"])
    assert overview.load(file_row["id"]) is not None

    segment = transcripts.list_segments(file_row["id"])[0]
    transcripts.update_segment(segment["id"], {"text": "ganz anderer Inhalt"})

    assert overview.load(file_row["id"]) is None
    run(file_row["id"])
    assert len(llm.of("overview")) == 2


def test_a_failed_describing_call_is_made_again_next_time(tmp_path, monkeypatch):
    """The digests are the expensive part and are kept — but a recording must
    not lose its title for good because one answer went wrong."""
    file_row = make_file(tmp_path)
    attempts: list[int] = []

    def describe(_user: str) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise pipeline.llm.LLMError("endpoint kaputt")
        return json.dumps({"title": "Quartalsbesprechung Vertrieb", "terms": ["Wesner"]})

    llm = Recorder(overview=describe)
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run(file_row["id"])  # the cleanup itself survives a missing overview
    assert pipeline.get_text(file_row["id"], "cleanup")["content"]
    assert overview.load(file_row["id"]).title == ""
    digest_calls = len(llm.of("digest"))

    run(file_row["id"])

    assert len(llm.of("digest")) == digest_calls  # the reading was kept
    assert overview.load(file_row["id"]).title == "Quartalsbesprechung Vertrieb"
    assert workspace.get_file(file_row["id"])["title"] == "Quartalsbesprechung Vertrieb"


def test_without_an_overview_a_shortened_section_fails_at_once(tmp_path, monkeypatch):
    """Asking again only helps when there was an orientation to leave out."""
    file_row = make_file(tmp_path, segments=4)
    llm = Recorder(cleanup="Kurz gesagt: es ging um viel.")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)
    segments = transcripts.list_segments(file_row["id"])

    with pytest.raises(RuntimeError, match="zusammengefasst"):
        pipeline.cleanup_segments(segments, "", "", NO_CANCEL, lambda p, m="": None)

    assert len(llm.of("cleanup")) == 1  # no identical second request


def test_a_changed_transcript_type_is_read_again(tmp_path, monkeypatch):
    """A digest keeps what its type will need — minutes need the decisions."""
    file_row = make_file(tmp_path, type_key="speech")
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)
    run(file_row["id"])

    protocol = next(t for t in project_types.list_types() if t["key"] == "protocol")
    workspace.update_project(file_row["project_id"], {"type_id": protocol["id"]})
    run(file_row["id"])

    assert len(llm.of("overview")) == 2
