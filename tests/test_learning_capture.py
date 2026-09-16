"""What a text looked like before the user corrected it.

Verba writes a transcript, cleans it up and translates it; the user corrects
the result. Until now that correction replaced the model's wording in place
and the pair was gone — and the pair is the only material a later step could
learn a correction from (docs/LEARNING.md, phase 1).

So the first user edit puts the model's own version aside, and only the
first: a second edit must not overwrite it with the user's own earlier one.
Everything here is about that "exactly once", and about the cases that look
like a correction without being one.

Which half is *learned* from was settled afterwards: the derived texts, never
the segments (`tests/test_learning_terms.py`). The segment pair is still kept
and still pinned here — it is the only record of what the recognition itself
heard.
"""

from __future__ import annotations

import pytest

from verba import config, db
from verba.services import pipeline, transcripts, whisper, workspace


@pytest.fixture(autouse=True)
def _data_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()


@pytest.fixture()
def file_id(tmp_path) -> int:
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("P")
    [file_row] = workspace.import_paths(project, [str(source)])
    return file_row["id"]


def add_segment(file_id: int, text: str, idx: int = 0, start: float = 0.0) -> int:
    """A segment as the transcription writes it — no correction markers."""
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            (file_id, idx, start, start + 10.0, text),
        )
        return int(cursor.lastrowid)


def row(segment_id: int) -> dict:
    return transcripts.get_segment(segment_id)


# ── derived texts: cleanup and translation ───────────────────────────


def test_a_derived_text_starts_without_a_correction(file_id):
    pipeline.save_text(file_id, "cleanup", "Vom Modell.")
    stored = pipeline.get_text(file_id, "cleanup")
    assert stored["model_content"] == ""
    assert stored["edited_at"] == ""


def test_the_first_edit_keeps_the_model_version(file_id):
    pipeline.save_text(file_id, "cleanup", "So sagt der Heilige Geist.")
    pipeline.update_text_content(file_id, "cleanup", "", "So spricht der Heilige Geist.")

    stored = pipeline.get_text(file_id, "cleanup")
    assert stored["content"] == "So spricht der Heilige Geist."
    assert stored["model_content"] == "So sagt der Heilige Geist."
    assert stored["edited_at"] != ""


def test_a_second_edit_does_not_overwrite_it(file_id):
    pipeline.save_text(file_id, "cleanup", "Vom Modell.")
    pipeline.update_text_content(file_id, "cleanup", "", "Erste Korrektur.")
    pipeline.update_text_content(file_id, "cleanup", "", "Zweite Korrektur.")

    stored = pipeline.get_text(file_id, "cleanup")
    assert stored["content"] == "Zweite Korrektur."
    # not "Erste Korrektur." — that one is the user's, not the model's
    assert stored["model_content"] == "Vom Modell."


def test_a_translation_is_tracked_per_language(file_id):
    pipeline.save_text(file_id, "translation", "Thus says the Lord.", language="en")
    pipeline.save_text(file_id, "translation", "Так говорит Господь.", language="ru")
    pipeline.update_text_content(file_id, "translation", "en", "Thus saith the Lord.")

    assert pipeline.get_text(file_id, "translation", "en")["model_content"] == "Thus says the Lord."
    assert pipeline.get_text(file_id, "translation", "ru")["model_content"] == ""


def test_running_the_step_again_starts_over(file_id):
    """A new model run pairs with nothing that came before it.

    The stored version belonged to a text that no longer exists; keeping it
    would offer a delta between two strangers.
    """
    pipeline.save_text(file_id, "cleanup", "Erster Lauf.")
    pipeline.update_text_content(file_id, "cleanup", "", "Korrigiert.")
    pipeline.save_text(file_id, "cleanup", "Zweiter Lauf.")

    stored = pipeline.get_text(file_id, "cleanup")
    assert stored["content"] == "Zweiter Lauf."
    assert stored["model_content"] == ""
    assert stored["edited_at"] == ""


# ── segments: the transcription itself ───────────────────────────────


def test_the_first_text_edit_keeps_the_transcription(file_id):
    segment_id = add_segment(file_id, "Gemeinde Bergstrasse")
    transcripts.update_segment(segment_id, {"text": "Gemeinde Bergstraße"})

    assert row(segment_id)["text_original"] == "Gemeinde Bergstrasse"
    assert row(segment_id)["edited_at"] != ""


def test_a_second_text_edit_does_not_overwrite_it(file_id):
    segment_id = add_segment(file_id, "Jahwe")
    transcripts.update_segment(segment_id, {"text": "Jahve"})
    transcripts.update_segment(segment_id, {"text": "Yahweh"})

    assert row(segment_id)["text"] == "Yahweh"
    assert row(segment_id)["text_original"] == "Jahwe"


def test_renaming_a_speaker_is_not_a_text_correction(file_id):
    """Otherwise the frozen "before" equals the "after" from then on, and no
    real correction of that segment could ever be recorded."""
    segment_id = add_segment(file_id, "Guten Morgen")
    transcripts.update_segment(segment_id, {"speaker": "Frau Berger"})
    assert row(segment_id)["text_original"] == ""
    assert row(segment_id)["edited_at"] == ""

    transcripts.rename_speaker(file_id, "Frau Berger", "Berger")
    assert row(segment_id)["text_original"] == ""

    # and the real correction afterwards still lands
    transcripts.update_segment(segment_id, {"text": "Guten Morgen!"})
    assert row(segment_id)["text_original"] == "Guten Morgen"


def test_moving_a_segment_is_not_a_text_correction(file_id):
    segment_id = add_segment(file_id, "Guten Morgen")
    transcripts.update_segment(segment_id, {"start_s": 1.5, "end_s": 4.0})
    assert row(segment_id)["edited_at"] == ""


def test_a_segment_the_user_typed_claims_no_transcription(file_id):
    """Nobody transcribed it, so a later edit of it must not present the
    user's own first wording as what the recognition heard."""
    segment = transcripts.create_segment(file_id, 0.0, 5.0, text="Selbst getippt")
    assert segment["edited_at"] != ""

    transcripts.update_segment(segment["id"], {"text": "Selbst getippt, korrigiert"})
    assert row(segment["id"])["text_original"] == ""


# ── everything that rewrites a transcript wholesale ──────────────────


def test_transcribing_again_drops_the_correction(file_id):
    segment_id = add_segment(file_id, "Erste Erkennung")
    transcripts.update_segment(segment_id, {"text": "Korrigiert"})

    whisper._store_segments(file_id, [{"start": 0.0, "end": 10.0, "text": "Zweite Erkennung"}])

    [fresh] = transcripts.list_segments(file_id)
    assert row(fresh["id"])["text_original"] == ""
    assert row(fresh["id"])["edited_at"] == ""


def test_a_speaker_split_carries_no_correction(file_id):
    """A segment cut in two is not the segment the correction belonged to."""
    segment_id = add_segment(file_id, "Ja. Und Sie?")
    transcripts.update_segment(segment_id, {"text": "Ja. Und Sie denn?"})

    transcripts.apply_speaker_plan(
        file_id,
        [
            {
                "id": segment_id,
                "pieces": [
                    {"start_s": 0.0, "end_s": 5.0, "text": "Ja.", "speaker": "Sprecher 1"},
                    {
                        "start_s": 5.0,
                        "end_s": 10.0,
                        "text": "Und Sie denn?",
                        "speaker": "Sprecher 2",
                    },
                ],
            }
        ],
    )

    pieces = transcripts.list_segments(file_id)
    assert len(pieces) == 2
    assert [row(piece["id"])["text_original"] for piece in pieces] == ["", ""]


def test_a_segment_that_only_gains_a_speaker_keeps_its_correction(file_id):
    """One voice, one row: the text is untouched, so the pair still holds."""
    segment_id = add_segment(file_id, "Ja, genau")
    transcripts.update_segment(segment_id, {"text": "Ja, genau!"})

    transcripts.apply_speaker_plan(
        file_id,
        [
            {
                "id": segment_id,
                "pieces": [
                    {"start_s": 0.0, "end_s": 10.0, "text": "Ja, genau!", "speaker": "Sprecher 1"}
                ],
            }
        ],
    )

    assert row(segment_id)["speaker"] == "Sprecher 1"
    assert row(segment_id)["text_original"] == "Ja, genau"


# ── upgrading an installation that predates all of this ──────────────


NEW_COLUMNS = (
    ("segments", "text_original"),
    ("segments", "edited_at"),
    ("derived_texts", "model_content"),
    ("derived_texts", "edited_at"),
)


def test_an_existing_database_gains_the_columns(file_id):
    """`_SCHEMA` runs first and leaves an existing table alone, so these
    columns can only come from the migration — with the rows still there."""
    segment_id = add_segment(file_id, "Aus der alten Version")
    pipeline.save_text(file_id, "cleanup", "Auch aus der alten Version.")
    with db.get_conn() as conn:
        for table, column in NEW_COLUMNS:
            # the names come from the literal tuple above, never from input
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        remaining = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
    assert "text_original" not in remaining

    db.init_db()

    with db.get_conn() as conn:
        for table, column in NEW_COLUMNS:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert column in columns, f"{table}.{column} was not migrated"
    assert row(segment_id)["text"] == "Aus der alten Version"
    assert row(segment_id)["text_original"] == ""
    assert pipeline.get_text(file_id, "cleanup")["model_content"] == ""

    # and it collects from the next correction on
    transcripts.update_segment(segment_id, {"text": "Korrigiert"})
    assert row(segment_id)["text_original"] == "Aus der alten Version"
