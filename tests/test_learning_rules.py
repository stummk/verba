"""Rules distilled from corrected texts (docs/LEARNING.md, phase 3).

A spelling is a word pair and needs no model. A rule — "keep the filler
words" — is a generalisation over several corrections, and only an LLM can
write one. Which is why most of this file is about the three things standing
between what the model answers and what a person ever sees:

- it must hold for several of the examples,
- it must speak about wording rather than quote the material,
- and nothing is used before somebody has accepted it.

The third one is what phase 4 will honour; here it is enough that a fresh
rule arrives as a proposal and never as a fact.
"""

from __future__ import annotations

import json
import threading

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import auth, learning, llm, pipeline, workspace

NO_CANCEL = threading.Event()


def no_report(_percent: int = 0, _message: str = "") -> None:
    pass


@pytest.fixture(autouse=True)
def _data_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.llm.mode = "openai"
    settings.llm.base_url = "https://api.example.com/v1"
    settings.llm.model = "m"
    config.save_settings(settings)
    db.init_db()
    # registered by create_app(); these tests work below the app, and the
    # queue refuses a kind it does not know
    monkeypatch.setitem(job_queue._handlers, "learn_rules", learning.handle_learn_rules_job)


@pytest.fixture()
def project(tmp_path):
    return workspace.create_project("P")


def make_file(project, tmp_path, name: str) -> dict:
    source = tmp_path / name
    source.write_bytes(b"x")
    [row] = workspace.import_paths(workspace.get_project(project["id"]), [str(source)])
    return workspace.get_file(row["id"])


def correct(file_row: dict, machine: str, human: str, kind: str = "cleanup") -> None:
    """A derived text as the model wrote it, and as the user corrected it."""
    pipeline.save_text(file_row["id"], kind, machine)
    pipeline.update_text_content(file_row["id"], kind, "", human)


def scope() -> int:
    return learning.INSTALLATION_SCOPE


def run_job(step: str = "cleanup") -> str:
    return learning.handle_learn_rules_job(
        {"payload": {"owner_id": scope(), "step": step}}, NO_CANCEL, no_report
    )


def answer(*rules: tuple[str, int]) -> str:
    return json.dumps([{"rule": text, "seen": seen} for text, seen in rules])


# ── reading the corrections ──────────────────────────────────────────


def test_only_the_changed_paragraphs_are_looked_at():
    passages = learning.changed_passages(
        "Gleich hier.\n\nÄhm, das war so.\n\nAuch gleich.",
        "Gleich hier.\n\nDas war so.\n\nAuch gleich.",
        600,
    )
    assert passages == [("Ähm, das war so.", "Das war so.")]


def test_an_added_paragraph_is_not_a_correction():
    """It has no counterpart to compare with, so on its own it is content."""
    assert learning.changed_passages("Eins.", "Eins.\n\nZwei.", 600) == []


def test_a_passage_is_cut_to_its_budget():
    [(was, now)] = learning.changed_passages("a" * 900, "b" * 900, 100)
    assert len(was) == len(now) == 100


def test_only_texts_that_were_actually_corrected_count(project, tmp_path):
    untouched = make_file(project, tmp_path, "a.mp3")
    pipeline.save_text(untouched["id"], "cleanup", "Nie angefasst.")
    corrected = make_file(project, tmp_path, "b.mp3")
    correct(corrected, "Ähm, so war das.", "So war das.")

    rows = learning.corrected_texts(scope(), "cleanup")
    assert [row["file_id"] for row in rows] == [corrected["id"]]


# ── the content boundary ─────────────────────────────────────────────


def test_a_rule_in_ordinary_language_passes():
    corpus = {"wesner": {1}, "predigt": {1, 2, 3}}
    assert learning.is_content_free("Entferne die Füllwörter", corpus, 3)


def test_a_rule_quoting_the_material_is_refused():
    """The word lives in one transcript, so it is that transcript's content —
    and under a shared scope it would stand in everyone's prompt."""
    corpus = {"wesner": {1}, "predigt": {1, 2, 3}}
    assert not learning.is_content_free("Schreibe Wesner immer aus", corpus, 3)


def test_a_word_spread_over_the_corpus_is_ordinary_language():
    corpus = {"predigt": {1, 2, 3}}
    assert learning.is_content_free("Nenne die Predigt beim Namen", corpus, 3)


def test_the_proposals_are_filtered_by_both_gates():
    corpus = {"wesner": {1}, "predigt": {1, 2, 3}}
    raw = answer(
        ("Entferne die Füllwörter", 4),  # kept
        ("Schreibe Wesner immer aus", 3),  # content
        ("Setze mehr Kommas", 1),  # too thin
    )
    assert learning.proposals(raw, corpus) == [("Entferne die Füllwörter", 4)]


def test_an_answer_that_is_not_json_yields_nothing():
    assert learning.proposals("Ich habe keine Regeln gefunden.", {}) == []


# ── storing them ─────────────────────────────────────────────────────


def test_a_rule_arrives_as_a_proposal():
    assert learning.record_rule(scope(), "cleanup", "Entferne die Füllwörter", 3)
    [rule] = learning.list_rules(scope())
    assert rule["status"] == "proposed"
    assert rule["evidence"] == 3
    assert rule["step"] == "cleanup"


def test_the_same_rule_found_again_is_confirmed_not_duplicated():
    learning.record_rule(scope(), "cleanup", "Entferne die Füllwörter", 2)
    learning.record_rule(scope(), "cleanup", "  entferne die   FÜLLWÖRTER  ", 5)
    [rule] = learning.list_rules(scope())
    assert rule["evidence"] == 5


def test_the_same_rule_for_the_other_step_is_its_own():
    learning.record_rule(scope(), "cleanup", "Entferne die Füllwörter", 2)
    learning.record_rule(scope(), "translate", "Entferne die Füllwörter", 2)
    assert len(learning.list_rules(scope())) == 2
    assert len(learning.list_rules(scope(), "cleanup")) == 1


def test_a_scope_keeps_only_its_best_attested_rules():
    settings = config.get_settings()
    settings.learning.max_rules = 2
    config.save_settings(settings)
    learning.record_rule(scope(), "cleanup", "Regel A", 5)
    learning.record_rule(scope(), "cleanup", "Regel B", 4)

    assert not learning.record_rule(scope(), "cleanup", "Regel C", 3)  # weaker, refused
    assert learning.record_rule(scope(), "cleanup", "Regel D", 9)  # stronger, displaces B

    assert sorted(rule["text"] for rule in learning.list_rules(scope())) == ["Regel A", "Regel D"]


def test_a_rule_can_be_reworded_and_judged():
    learning.record_rule(scope(), "cleanup", "Entferne die Füllwörter", 3)
    [rule] = learning.list_rules(scope())
    learning.update_rule(rule["id"], scope(), text="Behalte die Füllwörter", status="accepted")
    [changed] = learning.list_rules(scope())
    assert changed["text"] == "Behalte die Füllwörter"
    assert changed["status"] == "accepted"
    # the normalised form travels with the text, or the next run stores a twin
    learning.record_rule(scope(), "cleanup", "behalte die füllwörter", 9)
    assert len(learning.list_rules(scope())) == 1


# ── the job ──────────────────────────────────────────────────────────


def test_the_job_proposes_what_the_model_found(project, tmp_path, monkeypatch):
    for index in range(3):
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Ähm, das war so.\n\nGleich.", "Das war so.\n\nGleich.")

    monkeypatch.setattr(llm, "chat", lambda *_a, **_k: answer(("Entferne die Füllwörter", 3)))
    assert "1 Regel" in run_job()
    [rule] = learning.list_rules(scope())
    assert rule["text"] == "Entferne die Füllwörter"


def test_the_job_says_so_when_there_is_nothing_to_learn(project, tmp_path, monkeypatch):
    file_row = make_file(project, tmp_path, "a.mp3")
    correct(file_row, "Ähm, das war so.", "Das war so.")
    monkeypatch.setattr(llm, "chat", lambda *_a, **_k: answer())
    assert run_job() == learning.NOTHING_TO_LEARN


def test_the_job_asks_nothing_without_material(monkeypatch):
    """No corrected text, no call — the lane is not spent on an empty prompt."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("the LLM must not be called")

    monkeypatch.setattr(llm, "chat", refuse)
    assert run_job() == learning.NOTHING_TO_LEARN


def test_the_material_of_another_scope_stays_out(project, tmp_path, monkeypatch):
    for index in range(3):
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Ähm, das war so.", "Das war so.")
    seen: dict = {}
    monkeypatch.setattr(
        llm,
        "chat",
        lambda messages, **_k: seen.setdefault("user", messages[-1]["content"]) and "" or answer(),
    )
    learning.handle_learn_rules_job(
        {"payload": {"owner_id": 99, "step": "cleanup"}}, NO_CANCEL, no_report
    )
    assert "user" not in seen  # scope 99 owns nothing, so nothing was asked


# ── when it is scheduled ─────────────────────────────────────────────


def test_nothing_is_queued_before_enough_has_been_corrected(project, tmp_path):
    for index in range(2):  # below min_corrected_files (3)
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Ähm, das war so.", "Das war so.")
    assert job_queue.active_jobs("learn_rules") == []


def test_enough_corrections_queue_one_distillation(project, tmp_path):
    for index in range(3):
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Ähm, das war so.", "Das war so.")
    jobs = job_queue.active_jobs("learn_rules")
    assert len(jobs) == 1
    assert jobs[0]["payload"] == {"owner_id": scope(), "step": "cleanup"}


def test_a_second_correction_does_not_queue_a_second_job(project, tmp_path):
    for index in range(5):
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Ähm, das war so.", "Das war so.")
    assert len(job_queue.active_jobs("learn_rules")) == 1


def test_nothing_is_queued_without_an_llm(project, tmp_path):
    settings = config.get_settings()
    settings.llm.mode = "none"
    config.save_settings(settings)
    for index in range(3):
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Ähm, das war so.", "Das war so.")
    assert job_queue.active_jobs("learn_rules") == []


def test_nothing_is_queued_while_the_rules_are_off(project, tmp_path):
    settings = config.get_settings()
    settings.learning.rules_enabled = False
    config.save_settings(settings)
    for index in range(3):
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Ähm, das war so.", "Das war so.")
    assert job_queue.active_jobs("learn_rules") == []


def test_a_translation_is_its_own_step(project, tmp_path):
    for index in range(3):
        file_row = make_file(project, tmp_path, f"f{index}.mp3")
        correct(file_row, "Um, that was so.", "That was so.", kind="translation")
    [job] = job_queue.active_jobs("learn_rules")
    assert job["payload"]["step"] == "translate"


# ── the API ──────────────────────────────────────────────────────────


def test_the_api_lists_rewords_and_deletes(client):
    learning.record_rule(scope(), "cleanup", "Entferne die Füllwörter", 3)
    listed = client.get("/api/learning/rules").json()
    [rule] = listed["rules"]
    assert listed["enabled"] is True

    changed = client.put(f"/api/learning/rules/{rule['id']}", json={"status": "accepted"})
    assert changed.status_code == 200
    assert changed.json()["status"] == "accepted"

    assert client.delete(f"/api/learning/rules/{rule['id']}").status_code == 200
    assert client.get("/api/learning/rules").json()["rules"] == []


def test_the_api_refuses_a_rule_from_another_scope(client):
    learning.record_rule(99, "cleanup", "Fremde Regel", 3)
    assert client.get("/api/learning/rules").json()["rules"] == []
    assert client.put("/api/learning/rules/1", json={"status": "accepted"}).status_code == 404
    assert client.delete("/api/learning/rules/1").status_code == 404


def test_deleting_an_account_takes_its_rules_with_it():
    auth.enable("chef", "geheim1234")
    other = auth.create_user("zweiter", "geheim1234")
    learning.record_rule(other["id"], "cleanup", "Eigene Regel", 3)
    auth.delete_user(other["id"])
    assert learning.list_rules(other["id"]) == []
