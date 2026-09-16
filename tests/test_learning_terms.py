"""Learning the spellings a user keeps correcting (docs/LEARNING.md, phase 2).

Phase 1 kept what a text looked like before the user changed it. Here that
pair is read word by word — and most of this file is about what must *not*
be learned from it: a rewording is not a mishearing, a sentence rewrite is
not a spelling, and a word picked up from the first recording must not stay
the installation's truth once the user has written against it.
"""

from __future__ import annotations

import threading

import pytest

from verba import config, db
from verba.services import auth, learning, pipeline, transcripts, whisper, workspace

NO_CANCEL = threading.Event()


def no_report(_percent: int = 0, _message: str = "") -> None:
    pass


@pytest.fixture(autouse=True)
def _data_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()


@pytest.fixture()
def file_row(tmp_path):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("P")
    [row] = workspace.import_paths(project, [str(source)])
    with db.get_conn() as conn:
        conn.execute("UPDATE files SET language = 'de' WHERE id = ?", (row["id"],))
    return workspace.get_file(row["id"])


def add_segment(file_id: int, text: str, idx: int = 0) -> int:
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, 0, 10, ?)",
            (file_id, idx, text),
        )
        return int(cursor.lastrowid)


def terms() -> list[dict]:
    return learning.list_terms(learning.INSTALLATION_SCOPE)


def correct(file_row: dict, machine: str, human: str) -> None:
    """A cleanup as the model wrote it, and as the user corrected it."""
    pipeline.save_text(file_row["id"], "cleanup", machine)
    pipeline.update_text_content(file_row["id"], "cleanup", "", human)


# ── what counts as a correction ───────────────────────────────────────


def test_a_mishearing_is_a_correction():
    pairs = learning.word_pairs("Und Jahwe sprach", "Und Yahweh sprach", 0.5)
    assert pairs == [("Jahwe", "Yahweh")]


def test_a_rewording_is_not():
    """As a hint to the recognition "spricht" for "sagt" would be nonsense —
    nobody mishears one as the other."""
    assert learning.word_pairs("so sagt der Herr", "so spricht der Herr", 0.5) == []


def test_an_inserted_or_deleted_word_says_nothing_about_spelling():
    assert learning.word_pairs("ein Wort", "ein ganz anderes Wort", 0.5) == []
    assert learning.word_pairs("ein ganzes Wort hier", "ein Wort hier", 0.5) == []


def test_very_short_words_are_noise():
    assert learning.word_pairs("und da war er", "und wo war er", 0.5) == []


def test_a_wholesale_rewrite_teaches_nothing():
    """Retyping a segment replaces one block of words with another, and a
    block is not a spelling — only a single word against a single word is."""
    letters = "abcdefghijklmnopqrst"
    before = " ".join(f"wortform{c}" for c in letters)
    after = " ".join(f"wortfarm{c}" for c in letters)
    assert learning.word_pairs(before, after, 0.5) == []


def test_one_edit_contributes_at_most_a_handful():
    letters = "abcdefgh"
    before = " ".join(f"gleich wortform{c}" for c in letters)
    after = " ".join(f"gleich wortfarm{c}" for c in letters)
    assert len(learning.word_pairs(before, after, 0.5)) == learning.MAX_PAIRS_PER_EDIT


def test_a_capitalisation_is_a_correction():
    assert learning.word_pairs("der geist", "der Geist", 0.5) == [("geist", "Geist")]


# ── writing it down, and writing against it ──────────────────────────


def test_a_correction_is_recorded_with_its_language(file_row):
    learning.note_text_correction(file_row, "Gemeinde Bergstrasse", "Gemeinde Bergstraße")
    [term] = terms()
    assert (term["source"], term["target"]) == ("Bergstrasse", "Bergstraße")
    assert term["language"] == "de"
    assert term["evidence"] == 1
    assert term["status"] == "proposed"


def test_making_the_same_correction_again_confirms_it(file_row):
    for _ in range(3):
        learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    [term] = terms()
    assert term["evidence"] == 3
    assert term["counter_evidence"] == 0


def test_a_different_correction_of_the_same_word_is_counted_against_it(file_row):
    for _ in range(2):
        learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    learning.note_text_correction(file_row, "Jahwe sprach", "Jahve sprach")
    [term] = terms()
    assert term["target"] == "Yahweh"  # still the better attested one
    assert term["counter_evidence"] == 1


def test_the_newer_wording_takes_over_once_it_has_been_written_often_enough(file_row):
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    learning.note_text_correction(file_row, "Jahwe sprach", "Jahve sprach")
    [term] = terms()
    assert term["target"] == "Jahve"
    assert (term["evidence"], term["counter_evidence"]) == (1, 0)


def test_turning_a_correction_back_around_retires_it(file_row):
    """The way out of a wrong spelling picked up from the first recording."""
    learning.note_text_correction(file_row, "Meier kam", "Maier kam")
    learning.note_text_correction(file_row, "Maier kam", "Meier kam")
    [term] = terms()
    assert term["status"] == "retired"


def test_a_retired_term_comes_back_when_it_is_confirmed_again(file_row):
    learning.note_text_correction(file_row, "Meier kam", "Maier kam")
    learning.note_text_correction(file_row, "Maier kam", "Meier kam")
    learning.note_text_correction(file_row, "Meier kam", "Maier kam")
    [term] = terms()
    assert term["status"] == "proposed"


def test_nothing_is_learned_while_the_feature_is_off(file_row):
    settings = config.get_settings()
    settings.learning.enabled = False
    config.save_settings(settings)
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    assert terms() == []


# ── the hook: the processed text, the transcript only as a fallback ──


def test_correcting_the_cleanup_teaches_the_spelling(file_row):
    correct(file_row, "Und Jahwe sprach zur Gemeinde", "Und Yahweh sprach zur Gemeinde")
    assert [(t["source"], t["target"]) for t in terms()] == [("Jahwe", "Yahweh")]


def test_a_segment_teaches_while_there_is_no_cleanup(file_row):
    """Without a language model there is never a processed text, and without
    this the installation would learn nothing at all."""
    segment_id = add_segment(file_row["id"], "Und Jahwe sprach zur Gemeinde")
    transcripts.update_segment(segment_id, {"text": "Und Yahweh sprach zur Gemeinde"})
    assert [(t["source"], t["target"]) for t in terms()] == [("Jahwe", "Yahweh")]


def test_a_segment_falls_silent_once_a_cleanup_exists(file_row):
    """From then on the processed text is the source: a segment is where
    somebody retypes what they think they heard."""
    pipeline.save_text(file_row["id"], "cleanup", "Und Jahwe sprach zur Gemeinde")
    segment_id = add_segment(file_row["id"], "Und Jahwe sprach zur Gemeinde")
    transcripts.update_segment(segment_id, {"text": "Und Yahweh sprach zur Gemeinde"})
    assert terms() == []


def test_an_empty_cleanup_is_no_alternative(file_row):
    """A failed run leaves a row behind; it is not a text to learn from."""
    pipeline.save_text(file_row["id"], "cleanup", "   ")
    segment_id = add_segment(file_row["id"], "Und Jahwe sprach")
    transcripts.update_segment(segment_id, {"text": "Und Yahweh sprach"})
    assert len(terms()) == 1


def test_a_segment_the_user_typed_teaches_nothing(file_row):
    """Their own second thoughts about their own wording correct nothing."""
    segment = transcripts.create_segment(file_row["id"], 0.0, 5.0, text="Jahwe sprach")
    transcripts.update_segment(segment["id"], {"text": "Yahweh sprach"})
    assert terms() == []


def test_a_speaker_rename_teaches_nothing(file_row):
    segment_id = add_segment(file_row["id"], "Und Jahwe sprach")
    transcripts.update_segment(segment_id, {"speaker": "Sprecher 1"})
    assert terms() == []


def test_the_transcription_is_kept_either_way(file_row):
    """Even where it taught nothing: it is the only record of what was heard."""
    pipeline.save_text(file_row["id"], "cleanup", "Vom Modell.")
    segment_id = add_segment(file_row["id"], "Und Jahwe sprach")
    transcripts.update_segment(segment_id, {"text": "Und Yahweh sprach"})
    assert transcripts.get_segment(segment_id)["text_original"] == "Und Jahwe sprach"


def test_each_save_teaches_its_own_delta(file_row):
    """Deliberately not measured against the model's version: the editor saves
    while you type, and a pair re-derived on every save would count its own
    evidence up. An intermediate spelling never reaches the threshold on its
    own, so the noise stays below where it could apply."""
    correct(file_row, "Jahwe sprach", "Jahve sprach")
    pipeline.update_text_content(file_row["id"], "cleanup", "", "Yahweh sprach")
    assert sorted((t["source"], t["target"]) for t in terms()) == [
        ("Jahve", "Yahweh"),
        ("Jahwe", "Jahve"),
    ]
    assert all(t["evidence"] == 1 for t in terms())


def test_a_translation_teaches_in_its_target_language(file_row):
    """The corrected text is English, so the spelling is an English one — the
    recording's own language would hand it to the wrong transcription."""
    pipeline.save_text(file_row["id"], "translation", "Thus says Yahwe.", language="en")
    pipeline.update_text_content(file_row["id"], "translation", "en", "Thus says Yahweh.")
    [term] = terms()
    assert (term["language"], term["source"], term["target"]) == ("en", "Yahwe", "Yahweh")


def test_a_failure_while_learning_never_costs_the_edit(file_row, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(learning, "note_text_correction", boom)
    pipeline.save_text(file_row["id"], "cleanup", "Und Jahwe sprach")
    stored = pipeline.update_text_content(file_row["id"], "cleanup", "", "Und Yahweh sprach")
    assert stored["content"] == "Und Yahweh sprach"


# ── which of them apply ──────────────────────────────────────────────


def test_a_term_applies_only_once_it_has_been_seen_often_enough(file_row):
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    assert learning.active_terms(learning.INSTALLATION_SCOPE, "de") == []
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    assert len(learning.active_terms(learning.INSTALLATION_SCOPE, "de")) == 1


def test_accepting_a_term_applies_it_right_away(file_row):
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    [term] = terms()
    learning.update_term(term["id"], learning.INSTALLATION_SCOPE, status="accepted")
    assert len(learning.active_terms(learning.INSTALLATION_SCOPE, "de")) == 1


def test_a_rejected_term_never_applies_however_often_it_is_seen(file_row):
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    [term] = terms()
    learning.update_term(term["id"], learning.INSTALLATION_SCOPE, status="rejected")
    for _ in range(5):
        learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    assert learning.active_terms(learning.INSTALLATION_SCOPE, "de") == []


def test_another_language_does_not_borrow_the_spellings(file_row):
    for _ in range(2):
        learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    assert learning.active_terms(learning.INSTALLATION_SCOPE, "ru") == []
    assert len(learning.active_terms(learning.INSTALLATION_SCOPE, "de")) == 1
    # a recording whose language nobody knows may still use everything
    assert len(learning.active_terms(learning.INSTALLATION_SCOPE, "")) == 1


# ── handing it to the engines ────────────────────────────────────────


def test_the_hotwords_are_capped(file_row):
    settings = config.get_settings()
    settings.learning.max_hotwords = 2
    settings.learning.min_evidence = 1
    config.save_settings(settings)
    for letter in "abcde":
        learning.note_text_correction(file_row, f"wortform{letter}", f"wortfarm{letter}")
    assert len(learning.hotwords_for(file_row, "de").split(", ")) == 2


def test_the_transcription_is_told_the_learned_spellings(file_row, monkeypatch, tmp_path):
    settings = config.get_settings()
    settings.learning.min_evidence = 1
    config.save_settings(settings)
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")

    seen: dict = {}

    class FakeModel:
        def transcribe(self, _path, **kwargs):
            seen.update(kwargs)
            info = type("Info", (), {"duration": 10.0, "language": "de"})()
            return iter([]), info

    monkeypatch.setattr(whisper, "get_model", lambda **_kwargs: FakeModel())
    whisper._run_transcription(tmp_path / "a.mp3", file_row, file_row["id"], NO_CANCEL, no_report)
    assert seen["hotwords"] == "Yahweh"


def test_the_glossary_names_both_spellings(file_row):
    settings = config.get_settings()
    settings.learning.min_evidence = 1
    config.save_settings(settings)
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    block = learning.glossary_block(file_row)
    assert "Yahweh" in block and "Jahwe" in block


def test_the_glossary_stays_inside_its_budget(file_row):
    settings = config.get_settings()
    settings.learning.min_evidence = 1
    settings.learning.max_glossary_chars = 40
    config.save_settings(settings)
    for letter in "abcdefghijklmnopqrst":
        learning.note_text_correction(file_row, f"wortform{letter}", f"wortfarm{letter}")
    assert len(learning.glossary_block(file_row)) < 120


def test_nothing_is_handed_over_while_the_feature_is_off(file_row):
    settings = config.get_settings()
    settings.learning.min_evidence = 1
    config.save_settings(settings)
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    settings.learning.enabled = False
    config.save_settings(settings)
    assert learning.hotwords_for(file_row, "de") == ""
    assert learning.glossary_block(file_row) == ""


# ── scope: whose vocabulary is it ────────────────────────────────────


def test_without_user_management_everything_is_one_scope(file_row):
    assert learning.scope_for_file(file_row) == learning.INSTALLATION_SCOPE
    assert learning.scope_for_user(None) == learning.INSTALLATION_SCOPE


def test_with_user_management_each_account_learns_for_itself(tmp_path):
    admin = auth.enable("chef", "geheim1234")["user"]
    other = auth.create_user("zweiter", "geheim1234")
    source = tmp_path / "b.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("P2")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET owner_id = ?, visibility = 'private' WHERE id = ?",
            (admin["id"], project["id"]),
        )
    [row] = workspace.import_paths(workspace.get_project(project["id"]), [str(source)])

    assert learning.scope_for_file(workspace.get_file(row["id"])) == admin["id"]
    assert learning.scope_for_user(other) == other["id"]


def test_the_shared_switch_pools_them_again(tmp_path):
    admin = auth.enable("chef", "geheim1234")["user"]
    settings = config.get_settings()
    settings.learning.shared = True
    config.save_settings(settings)
    assert learning.scope_for_user(admin) == learning.INSTALLATION_SCOPE


def test_deleting_an_account_takes_its_vocabulary_with_it(tmp_path):
    admin = auth.enable("chef", "geheim1234")["user"]
    other = auth.create_user("zweiter", "geheim1234")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO learned_terms (owner_id, language, source, target) "
            "VALUES (?, 'de', 'Jahwe', 'Yahweh')",
            (other["id"],),
        )
    auth.delete_user(other["id"])
    assert learning.list_terms(other["id"]) == []
    assert admin["id"]  # the survivor keeps their own


# ── the API ──────────────────────────────────────────────────────────


def test_the_api_lists_corrects_and_forgets(client, file_row):
    learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    listed = client.get("/api/learning/terms").json()
    assert listed["min_evidence"] == config.get_settings().learning.min_evidence
    [term] = listed["terms"]

    updated = client.put(f"/api/learning/terms/{term['id']}", json={"target": "JHWH"})
    assert updated.status_code == 200
    assert updated.json()["target"] == "JHWH"

    assert client.delete(f"/api/learning/terms/{term['id']}").status_code == 200
    assert client.get("/api/learning/terms").json()["terms"] == []


def test_the_api_refuses_an_entry_from_another_scope(client, file_row):
    """The scope is the caller's own — a term id from elsewhere is a 404,
    not somebody else's vocabulary."""
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO learned_terms (owner_id, language, source, target) "
            "VALUES (99, 'de', 'Jahwe', 'Yahweh')"
        )
        foreign_id = int(cursor.lastrowid)
    assert client.get("/api/learning/terms").json()["terms"] == []
    rejected = client.put(f"/api/learning/terms/{foreign_id}", json={"status": "accepted"})
    assert rejected.status_code == 404
    assert client.delete(f"/api/learning/terms/{foreign_id}").status_code == 404
