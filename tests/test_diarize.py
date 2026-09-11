"""Speaker recognition: naming the voices, and cutting a segment in two.

The part worth testing is not the two ONNX models — it is what their answer
does to a transcript. A recognition result is a handful of turns
(`speaker 1 from 4.4 s to 6.9 s`), and from there on everything is arithmetic
over segments, word timings and text, which is exactly where a transcript
gets silently mangled:

- a segment that holds one voice must keep its row, its text and its timings
  and only gain a name;
- a segment in which the speaker changes must be cut *between two words*, with
  every character of it still there afterwards;
- a segment nobody was recognised in must be left alone, name and all.

So the tests run against `plan_speakers` (pure) and against the database
(`apply_speaker_plan`), and never against a model.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from verba import config, db
from verba.services import diarize, project_types, transcripts, workspace
from verba.services.diarize import Turn


def words(*items: tuple[float, float, str]) -> str:
    return transcripts.encode_words(list(items))


def segment(
    seg_id: int, start: float, end: float, text: str, word_timings: str = "", speaker: str = ""
) -> dict:
    return {
        "id": seg_id,
        "start_s": start,
        "end_s": end,
        "text": text,
        "speaker": speaker,
        "words": word_timings,
    }


# ── the names ─────────────────────────────────────────────────────────


def test_speakers_are_numbered_by_who_talks_first():
    """The clustering hands out arbitrary numbers — 0, 1, 6, 11 for four people."""
    turns = [Turn(0.0, 2.0, 6), Turn(2.0, 4.0, 11), Turn(4.0, 6.0, 6)]

    assert diarize.speaker_names(turns, "Sprecher") == {6: "Sprecher 1", 11: "Sprecher 2"}


def test_the_prefix_follows_the_interface_language():
    assert diarize.speaker_prefix("de") == "Sprecher"
    assert diarize.speaker_prefix("en") == "Speaker"
    assert diarize.speaker_prefix("ru-RU") == "Говорящий"
    assert diarize.speaker_prefix("kl") == "Sprecher"  # a language with no catalog


# ── one voice per segment: a name, and nothing else ───────────────────


def test_a_segment_of_one_voice_only_gets_its_name():
    turns = [Turn(0.0, 10.0, 0)]
    original = segment(1, 1.0, 3.0, "Ein ganzer Satz.", words((1.0, 3.0, " Ein ganzer Satz.")))

    [entry] = diarize.plan_speakers([original], turns)

    assert entry["id"] == 1
    [piece] = entry["pieces"]
    assert piece["speaker"] == "Sprecher 1"
    assert (piece["start_s"], piece["end_s"]) == (1.0, 3.0)
    assert piece["text"] == "Ein ganzer Satz."
    assert piece["words"] == original["words"]  # untouched


def test_a_segment_nobody_speaks_in_is_left_alone():
    """Music, silence, a passage the segmentation heard nobody in.

    An invented name would be worse than the one the user typed there.
    """
    turns = [Turn(20.0, 25.0, 0)]

    assert diarize.plan_speakers([segment(1, 1.0, 3.0, "…", speaker="Anna")], turns) == []


# ── two voices in one segment: the cut ────────────────────────────────


@pytest.fixture()
def straddling() -> dict:
    """One segment across a speaker change at 4.0 s, one word every 0.4 s."""
    timings = [(1.6 + i * 0.4, 2.0 + i * 0.4, f" wort{i}") for i in range(12)]
    return segment(
        1,
        1.6,
        6.4,
        "".join(word for _s, _e, word in timings).strip(),
        transcripts.encode_words(timings),
    )


def test_a_speaker_change_cuts_the_segment_between_two_words(straddling):
    turns = [Turn(0.0, 4.0, 0), Turn(4.0, 9.0, 1)]

    [entry] = diarize.plan_speakers([straddling], turns)
    first, second = entry["pieces"]

    assert first["speaker"] == "Sprecher 1"
    assert second["speaker"] == "Sprecher 2"
    assert first["text"] == "wort0 wort1 wort2 wort3 wort4 wort5"
    assert second["text"] == "wort6 wort7 wort8 wort9 wort10 wort11"
    # nothing of the segment is lost or duplicated
    assert f"{first['text']} {second['text']}" == straddling["text"]


def test_the_pieces_cover_the_segment_without_a_gap(straddling):
    turns = [Turn(0.0, 4.0, 0), Turn(4.0, 9.0, 1)]

    [entry] = diarize.plan_speakers([straddling], turns)
    first, second = entry["pieces"]

    # the outer edges stay the segment's own — the first word may start after
    # the segment does, and that lead-in must not fall out of the timeline
    assert first["start_s"] == straddling["start_s"]
    assert second["end_s"] == straddling["end_s"]
    assert first["end_s"] == second["start_s"]


def test_each_piece_keeps_the_timings_of_its_own_words(straddling):
    turns = [Turn(0.0, 4.0, 0), Turn(4.0, 9.0, 1)]

    [entry] = diarize.plan_speakers([straddling], turns)

    kept = [transcripts.decode_words(piece["words"]) for piece in entry["pieces"]]
    assert [len(block) for block in kept] == [6, 6]
    assert [word for block in kept for _s, _e, word in block] == [f" wort{i}" for i in range(12)]


def test_a_flicker_too_short_to_be_a_turn_does_not_cut_anything(straddling):
    """A single word caught by the other voice's fingerprint is jitter.

    0.3 s of "somebody else" in the middle of a sentence is the recognition
    wobbling, not a turn — and cutting there would leave two segments that
    each end mid-clause.
    """
    turns = [Turn(0.0, 3.6, 0), Turn(3.6, 3.9, 1), Turn(3.9, 9.0, 0)]

    [entry] = diarize.plan_speakers([straddling], turns)

    assert len(entry["pieces"]) == 1
    assert entry["pieces"][0]["text"] == straddling["text"]


def test_three_voices_become_three_pieces(straddling):
    turns = [Turn(0.0, 3.0, 0), Turn(3.0, 5.0, 1), Turn(5.0, 9.0, 2)]

    [entry] = diarize.plan_speakers([straddling], turns)

    assert [piece["speaker"] for piece in entry["pieces"]] == [
        "Sprecher 1",
        "Sprecher 2",
        "Sprecher 3",
    ]
    assert "".join(piece["text"] for piece in entry["pieces"]).replace(" ", "") == straddling[
        "text"
    ].replace(" ", "")


# ── without word timings: the honest guess ────────────────────────────


def test_without_word_timings_the_cut_lands_on_a_sentence_end():
    """An older transcript has no timings — the text is cut by time instead.

    The position comes from the share of the duration that has passed and is
    then moved to the nearest sentence end, so the cut still falls between two
    words rather than inside one.
    """
    turns = [Turn(0.0, 5.0, 0), Turn(5.0, 10.0, 1)]
    original = segment(1, 0.0, 10.0, "Das ist meine Frage. Und das ist die Antwort darauf.")

    [entry] = diarize.plan_speakers([original], turns)
    first, second = entry["pieces"]

    assert first["text"] == "Das ist meine Frage."
    assert second["text"] == "Und das ist die Antwort darauf."
    assert first["words"] == "" and second["words"] == ""


def test_a_cut_that_would_leave_a_piece_without_text_keeps_the_segment_whole():
    turns = [Turn(0.0, 9.9, 0), Turn(9.9, 10.0, 1)]
    original = segment(1, 0.0, 10.0, "Nur ein Wort")

    [entry] = diarize.plan_speakers([original], turns)

    assert len(entry["pieces"]) == 1
    assert entry["pieces"][0]["text"] == "Nur ein Wort"


def test_timings_that_no_longer_describe_the_text_are_not_used_to_cut():
    """A text corrected by hand outgrows its timings.

    Cutting by them would throw the added characters away, so the segment
    falls back to the time-based split — which keeps every character.
    """
    turns = [Turn(0.0, 5.0, 0), Turn(5.0, 10.0, 1)]
    original = segment(
        1,
        0.0,
        10.0,
        "Das ist meine Frage. Und das ist die Antwort darauf.",
        words((0.0, 1.0, " Das")),  # all that is left of the timings
    )

    [entry] = diarize.plan_speakers([original], turns)

    assert "".join(piece["text"] for piece in entry["pieces"]).replace(" ", "") == original[
        "text"
    ].replace(" ", "")


# ── writing it into the transcript ────────────────────────────────────


@pytest.fixture()
def file_with_transcript(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    project = workspace.create_project("Interviews")
    source = tmp_path / "gespraech.mp3"
    source.write_bytes(b"x")
    [row] = workspace.import_paths(project, [str(source)])
    file_id = row["id"]
    transcripts.replace_all_segments(
        file_id,
        [
            {"start_s": 0.0, "end_s": 2.0, "text": "Guten Tag.", "speaker": ""},
            {
                "start_s": 2.0,
                "end_s": 6.0,
                "text": "Wie geht es Ihnen? Danke, gut.",
                "speaker": "",
                "words": words(
                    (2.0, 2.4, " Wie"),
                    (2.4, 2.8, " geht"),
                    (2.8, 3.2, " es"),
                    (3.2, 3.9, " Ihnen?"),
                    (4.4, 4.9, " Danke,"),
                    (4.9, 5.6, " gut."),
                ),
            },
        ],
    )
    return file_id


def test_the_recognition_names_and_splits_in_the_database(file_with_transcript):
    turns = [Turn(0.0, 4.0, 0), Turn(4.4, 8.0, 1)]

    result = diarize.apply_to_file(file_with_transcript, turns)

    rows = transcripts.list_segments(file_with_transcript)
    assert [row["text"] for row in rows] == [
        "Guten Tag.",
        "Wie geht es Ihnen?",
        "Danke, gut.",
    ]
    assert [row["speaker"] for row in rows] == ["Sprecher 1", "Sprecher 1", "Sprecher 2"]
    assert [row["idx"] for row in rows] == [0, 1, 2]  # renumbered, gap-free
    assert result["splits"] == 1
    assert result["speakers"] == 2


def test_a_named_segment_keeps_its_row(file_with_transcript):
    """Only the segments that are cut are replaced; the editor keeps its place."""
    before = {row["text"]: row["id"] for row in transcripts.list_segments(file_with_transcript)}

    diarize.apply_to_file(file_with_transcript, [Turn(0.0, 8.0, 0)])

    after = {row["text"]: row["id"] for row in transcripts.list_segments(file_with_transcript)}
    assert after == before


def test_a_speaker_can_be_renamed_everywhere_at_once(file_with_transcript):
    diarize.apply_to_file(file_with_transcript, [Turn(0.0, 4.0, 0), Turn(4.4, 8.0, 1)])

    changed = transcripts.rename_speaker(file_with_transcript, "Sprecher 1", "Frau Berger")

    assert changed == 2
    speakers = {row["speaker"] for row in transcripts.list_segments(file_with_transcript)}
    assert speakers == {"Frau Berger", "Sprecher 2"}


def test_the_json_copy_in_the_workspace_follows(file_with_transcript):
    diarize.apply_to_file(file_with_transcript, [Turn(0.0, 4.0, 0), Turn(4.4, 8.0, 1)])

    file_row = workspace.get_file(file_with_transcript)
    project = workspace.get_project(file_row["project_id"])
    written = json.loads(
        (workspace.project_dir(project) / "transcripts" / "gespraech.json").read_text(
            encoding="utf-8"
        )
    )

    assert [entry["speaker"] for entry in written["segments"]] == [
        "Sprecher 1",
        "Sprecher 1",
        "Sprecher 2",
    ]


def test_editing_a_text_drops_its_word_timings(file_with_transcript):
    """The timings described the words that were there — not the new ones."""
    row = transcripts.list_segments(file_with_transcript)[1]
    assert row["words"]

    updated = transcripts.update_segment(row["id"], {"text": "Ganz anders."})

    assert updated["words"] == ""


# ── how many voices there are ─────────────────────────────────────────


def test_nothing_states_how_many_speakers_there_are():
    """The count is always worked out from the recording, never given.

    Guarded rather than assumed: a number is the strongest hint the clustering
    can get, which makes it tempting to offer a field for it — and a number
    left over from another recording is worse than no number at all.
    """
    assert "speakers" not in config.DiarizationSettings.model_fields
    assert not hasattr(diarize, "speaker_count_for")
    assert "speaker_count" not in workspace.UPDATABLE_FILE_FIELDS


# ── the transcript type decides whether it runs at all ────────────────


def test_the_conversation_types_recognise_speakers_and_the_single_voices_do_not(tmp_path):
    db.init_db()
    project_types.seed_builtin_types()

    by_key = {entry["key"]: entry for entry in project_types.list_types()}
    for key in ("interview", "protocol", "roleplay"):
        assert project_types.diarizes({"type_diarize": by_key[key]["diarize"]}), key
    for key in ("song", "poem", "speech"):
        assert not project_types.diarizes({"type_diarize": by_key[key]["diarize"]}), key


def test_a_project_without_a_type_recognises_nothing():
    assert not project_types.diarizes({})
    assert not project_types.diarizes({"type_diarize": None})


# ── an installation written before any of this existed ────────────────

LEGACY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE project_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    output_prompt TEXT NOT NULL DEFAULT '',
    structure TEXT NOT NULL DEFAULT 'paragraphs',
    verbatim INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL,
    type_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    duration REAL,
    language TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (file_id, idx)
);
"""


@pytest.fixture()
def legacy_db(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    conn = sqlite3.connect(db.db_path())
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO projects (id, name, slug, workspace) VALUES (1, 'Alt', 'alt', ?)",
        (str(tmp_path / "workspaces" / "alt"),),
    )
    conn.execute(
        "INSERT INTO files (id, project_id, filename, rel_path, status) "
        "VALUES (1, 1, 'alt.mp3', 'audio/alt.mp3', 'done')"
    )
    conn.execute(
        "INSERT INTO segments (file_id, idx, start_s, end_s, text) "
        "VALUES (1, 0, 0.0, 2.0, 'Erster Satz')"
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_an_older_database_gains_the_new_columns(legacy_db):
    db.init_db()

    with db.get_conn() as conn:
        segments = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
        types = {row["name"] for row in conn.execute("PRAGMA table_info(project_types)")}

    assert "words" in segments
    assert "diarize" in types
    # and the transcript that was there is still there
    assert transcripts.list_segments(1)[0]["text"] == "Erster Satz"
    assert transcripts.list_segments(1)[0]["words"] == ""


def test_builtin_types_written_before_the_switch_get_it_backfilled(legacy_db):
    """The seed marker is already set, so only the per-field backfill runs."""
    db.init_db()
    with db.get_conn() as conn:
        db.set_meta(conn, project_types.SEED_MARKER, "1")
        conn.execute(
            "INSERT INTO project_types (key, name, system_prompt, builtin) "
            "VALUES ('interview', 'Interview/Dialogue', 'x', 1)"
        )
    project_types.seed_builtin_types()

    assert project_types.get_type_by_key("interview")["diarize"] == 1
