"""The learned rules reach the prompts (docs/LEARNING.md, phase 4).

Phase 3 produced proposals that did nothing. Here an accepted rule is put in
front of the cleanup and the translation — under three conditions that are
what this file is about: it must fit a budget the section needs more than it
does, what a run was given must be written down, and a text that had to be
corrected anyway counts against the rules it was written under.

The oldest promise of the pipeline is not touched by any of it: a type that
reproduces its material still has to hand every sentence back.
"""

from __future__ import annotations

import threading

import pytest

from verba import config, db
from verba.services import learning, llm, pipeline, project_types, workspace

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
    project_types.seed_builtin_types()


@pytest.fixture()
def file_row(tmp_path):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("P")
    [row] = workspace.import_paths(project, [str(source)])
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            [(row["id"], i, i * 30.0, (i + 1) * 30.0, wording(i)) for i in range(2)],
        )
    return workspace.get_file(row["id"])


def wording(index: int) -> str:
    """Enough distinct words that the guard against a summary can judge."""
    return f"Abschnitt {index} " + " ".join(f"begriff{index}n{n}" for n in range(60))


def accept(step: str, text: str) -> int:
    learning.record_rule(learning.INSTALLATION_SCOPE, step, text, 3)
    [rule] = [
        r for r in learning.list_rules(learning.INSTALLATION_SCOPE, step) if r["text"] == text
    ]
    learning.update_rule(rule["id"], learning.INSTALLATION_SCOPE, status="accepted")
    return rule["id"]


class Echo:
    """An LLM that hands the material back and remembers every system prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, messages, model_override="", **kwargs):
        self.prompts.append(messages[0]["content"])
        return messages[-1]["content"]


# ── the rules reach the prompt ───────────────────────────────────────


def test_an_accepted_rule_is_put_in_front_of_the_cleanup(file_row, monkeypatch):
    accept("cleanup", "Behalte die Füllwörter")
    echo = Echo()
    monkeypatch.setattr(llm, "chat", echo)

    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)

    assert any("Behalte die Füllwörter" in prompt for prompt in echo.prompts)


def test_a_proposal_is_not_an_instruction(file_row, monkeypatch):
    """However well attested — that is the difference between a spelling,
    which is a fact about a word, and a rule, which is a judgement."""
    learning.record_rule(learning.INSTALLATION_SCOPE, "cleanup", "Behalte die Füllwörter", 9)
    echo = Echo()
    monkeypatch.setattr(llm, "chat", echo)

    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)

    assert not any("Behalte die Füllwörter" in prompt for prompt in echo.prompts)


def test_a_rule_of_the_other_step_stays_out(file_row, monkeypatch):
    accept("translate", "Übersetze Zitate in der Bibelsprache")
    echo = Echo()
    monkeypatch.setattr(llm, "chat", echo)

    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)

    assert not any("Bibelsprache" in prompt for prompt in echo.prompts)


def test_the_translation_gets_its_own_rules(file_row, monkeypatch):
    accept("translate", "Übersetze Zitate in der Bibelsprache")
    echo = Echo()
    monkeypatch.setattr(llm, "chat", echo)

    rules, used = learning.rules_block(file_row, "translate")
    pipeline.run_translation(
        file_row["id"],
        "Ein Satz.",
        "en",
        "",
        "",
        NO_CANCEL,
        no_report,
        rules=rules,
        rules_used=used,
    )

    assert any("Bibelsprache" in prompt for prompt in echo.prompts)


def test_nothing_is_added_while_the_rules_are_off(file_row, monkeypatch):
    accept("cleanup", "Behalte die Füllwörter")
    settings = config.get_settings()
    settings.learning.rules_enabled = False
    config.save_settings(settings)
    echo = Echo()
    monkeypatch.setattr(llm, "chat", echo)

    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)

    assert not any("Behalte die Füllwörter" in prompt for prompt in echo.prompts)


# ── the budget ───────────────────────────────────────────────────────


def test_the_block_stays_inside_its_budget(file_row):
    settings = config.get_settings()
    settings.learning.max_glossary_chars = 60
    config.save_settings(settings)
    for index in range(10):
        accept("cleanup", f"Regel Nummer {index} mit etwas Text dahinter")

    block, used = learning.rules_block(file_row, "cleanup")
    assert len(block) < 60 + len(learning.RULES_HEADING)
    assert 0 < len(used) < 10


def test_a_model_that_had_to_be_split_loses_the_rules_first(file_row):
    """Every character of advice is one the section itself does not get, and
    the section has to come back complete."""
    limit = pipeline.SizeLimit()
    assert pipeline._within_budget("mit Regeln", "ohne", limit) == "mit Regeln"
    limit.shrink_to(1000)
    assert pipeline._within_budget("mit Regeln", "ohne", limit) == "ohne"


# ── what a run was given ─────────────────────────────────────────────


def test_the_run_writes_down_which_rules_it_had(file_row, monkeypatch):
    rule_id = accept("cleanup", "Behalte die Füllwörter")
    monkeypatch.setattr(llm, "chat", Echo())

    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)

    assert pipeline.get_text(file_row["id"], "cleanup")["rules_used"] == str(rule_id)


def test_a_run_without_rules_records_none(file_row, monkeypatch):
    monkeypatch.setattr(llm, "chat", Echo())
    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)
    assert pipeline.get_text(file_row["id"], "cleanup")["rules_used"] == ""


# ── a correction counts against them ─────────────────────────────────


def test_correcting_the_text_counts_against_the_rules_it_used(file_row, monkeypatch):
    rule_id = accept("cleanup", "Behalte die Füllwörter")
    monkeypatch.setattr(llm, "chat", Echo())
    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)

    pipeline.update_text_content(file_row["id"], "cleanup", "", "Von Hand anders.")

    assert learning.get_rule(rule_id, learning.INSTALLATION_SCOPE)["counter_evidence"] == 1


def test_a_rule_written_against_often_enough_steps_aside(file_row, monkeypatch):
    rule_id = accept("cleanup", "Behalte die Füllwörter")  # evidence 3
    monkeypatch.setattr(llm, "chat", Echo())
    pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)

    for index in range(4):  # one more than the evidence behind it
        pipeline.update_text_content(file_row["id"], "cleanup", "", f"Fassung {index}")

    rule = learning.get_rule(rule_id, learning.INSTALLATION_SCOPE)
    assert rule["counter_evidence"] == 4
    assert rule["status"] == "retired"
    assert learning.active_rules(learning.INSTALLATION_SCOPE, "cleanup") == []


def test_a_text_written_without_rules_blames_nobody(file_row, monkeypatch):
    rule_id = accept("cleanup", "Behalte die Füllwörter")
    monkeypatch.setattr(llm, "chat", Echo())
    pipeline.save_text(file_row["id"], "cleanup", "Ohne Regeln entstanden.")

    pipeline.update_text_content(file_row["id"], "cleanup", "", "Korrigiert.")

    assert learning.get_rule(rule_id, learning.INSTALLATION_SCOPE)["counter_evidence"] == 0


# ── the promise the pipeline already made ────────────────────────────


def test_a_rule_cannot_turn_a_reproducing_type_into_a_summary(file_row, monkeypatch):
    """The guard is measured against the material, so no instruction — learned
    or typed — gets past it."""
    accept("cleanup", "Fasse alles knapp zusammen")
    monkeypatch.setattr(llm, "chat", lambda *_a, **_k: "Kurz.")

    with pytest.raises(RuntimeError, match="zusammengefasst"):
        pipeline.run_cleanup(file_row["id"], "", "", NO_CANCEL, no_report)


# ── the type's own translation prompt ────────────────────────────────


def test_a_type_without_one_translates_by_the_default(file_row, monkeypatch):
    echo = Echo()
    monkeypatch.setattr(llm, "chat", echo)

    pipeline.run_translation(file_row["id"], "Ein Satz.", "en", "", "", NO_CANCEL, no_report)

    assert project_types.DEFAULT_TRANSLATE_PROMPT[:40] in echo.prompts[0]


def test_a_type_can_say_how_its_translations_sound(file_row, monkeypatch):
    echo = Echo()
    monkeypatch.setattr(llm, "chat", echo)

    pipeline.run_translation(
        file_row["id"],
        "Ein Satz.",
        "en",
        "",
        "",
        NO_CANCEL,
        no_report,
        translate_prompt="Übersetze frei und modern.",
    )

    assert "Übersetze frei und modern." in echo.prompts[0]
    assert project_types.DEFAULT_TRANSLATE_PROMPT[:40] not in echo.prompts[0]


def test_the_builtins_carry_the_default_so_it_can_be_edited():
    for row in project_types.list_types(include_legacy=False):
        assert row["translate_prompt"] == project_types.DEFAULT_TRANSLATE_PROMPT


def test_a_new_type_starts_from_the_default():
    created = project_types.create_type("Eigener", "Bereinige.")
    assert created["translate_prompt"] == project_types.DEFAULT_TRANSLATE_PROMPT


def test_the_api_offers_the_default_and_takes_a_replacement(client):
    defaults = client.get("/api/types/defaults").json()
    assert defaults["translate_prompt"] == project_types.DEFAULT_TRANSLATE_PROMPT

    created = client.post(
        "/api/types",
        json={
            "name": "Lied frei",
            "system_prompt": "Bereinige.",
            "translate_prompt": "Übersetze singbar.",
        },
    ).json()
    assert created["translate_prompt"] == "Übersetze singbar."
