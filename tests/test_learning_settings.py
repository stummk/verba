"""Switching the learning off, and starting over (docs/LEARNING.md, phase 5).

Two promises. Off means off — nothing new is learned and nothing already
learned reaches a prompt, which is the earlier behaviour exactly. And there is
a way back from a vocabulary that went wrong early, without touching the
transcripts it came from.
"""

from __future__ import annotations

import pytest

from verba import config, db
from verba.services import auth, learning, workspace


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


def learn_something(file_row) -> None:
    for _ in range(2):
        learning.note_text_correction(file_row, "Jahwe sprach", "Yahweh sprach")
    learning.record_rule(learning.INSTALLATION_SCOPE, "cleanup", "Behalte die Füllwörter", 3)


# ── the switches ─────────────────────────────────────────────────────


def test_the_settings_carry_the_switches():
    settings = config.get_settings().learning
    assert settings.enabled is True
    assert settings.rules_enabled is True
    assert settings.shared is False


def test_off_means_nothing_new_and_nothing_applied(file_row):
    learn_something(file_row)
    learning.update_rule(
        learning.list_rules(learning.INSTALLATION_SCOPE)[0]["id"],
        learning.INSTALLATION_SCOPE,
        status="accepted",
    )
    assert learning.hotwords_for(file_row, "de")  # on, it would be used

    settings = config.get_settings()
    settings.learning.enabled = False
    config.save_settings(settings)

    assert learning.hotwords_for(file_row, "de") == ""
    assert learning.glossary_block(file_row) == ""
    assert learning.rules_block(file_row, "cleanup") == ("", [])
    learning.note_text_correction(file_row, "Meier kam", "Maier kam")
    assert len(learning.list_terms(learning.INSTALLATION_SCOPE)) == 1  # nothing added


def test_the_rules_can_be_switched_off_on_their_own(file_row):
    """For whoever wants the spellings without paying LLM calls for rules."""
    learn_something(file_row)
    learning.update_rule(
        learning.list_rules(learning.INSTALLATION_SCOPE)[0]["id"],
        learning.INSTALLATION_SCOPE,
        status="accepted",
    )
    settings = config.get_settings()
    settings.learning.rules_enabled = False
    config.save_settings(settings)

    assert learning.rules_block(file_row, "cleanup") == ("", [])
    assert learning.hotwords_for(file_row, "de")  # the spellings stay


def test_the_api_takes_the_switches(client):
    settings = client.get("/api/settings").json()
    settings["learning"]["enabled"] = False
    settings["learning"]["min_evidence"] = 5
    assert client.put("/api/settings", json=settings).status_code == 200

    stored = config.get_settings().learning
    assert stored.enabled is False
    assert stored.min_evidence == 5


def test_a_normal_user_is_not_offered_them(client):
    """They are installation-wide, so they belong to an administrator."""
    client.post("/api/auth/enable", json={"username": "chef", "password": "geheim1234"})
    auth.create_user("zweiter", "geheim1234", must_change_password=False)
    client.post("/api/auth/login", json={"username": "zweiter", "password": "geheim1234"})

    reduced = client.get("/api/settings").json()
    assert reduced["restricted"] is True
    assert "learning" not in reduced


# ── starting over ────────────────────────────────────────────────────


def test_forgetting_a_scope_takes_spellings_and_rules(file_row):
    learn_something(file_row)
    gone = learning.forget_scope(learning.INSTALLATION_SCOPE)

    assert gone == {"terms": 1, "rules": 1}
    assert learning.list_terms(learning.INSTALLATION_SCOPE) == []
    assert learning.list_rules(learning.INSTALLATION_SCOPE) == []


def test_forgetting_lets_the_distillation_start_again(file_row):
    """The marker for "already looked at this" goes too, or the next run would
    silently skip the corrections it has seen before."""
    learning._mark_run(learning.INSTALLATION_SCOPE, "cleanup")
    assert learning._last_run(learning.INSTALLATION_SCOPE, "cleanup")

    learning.forget_scope(learning.INSTALLATION_SCOPE)

    assert learning._last_run(learning.INSTALLATION_SCOPE, "cleanup") == ""


def test_the_transcripts_survive_it(file_row):
    learn_something(file_row)
    learning.forget_scope(learning.INSTALLATION_SCOPE)
    assert workspace.get_file(file_row["id"]) is not None


def test_the_api_forgets_only_the_callers_scope(client, file_row):
    learn_something(file_row)
    learning.record_rule(99, "cleanup", "Fremde Regel", 3)

    response = client.request("DELETE", "/api/learning")
    assert response.status_code == 200
    assert response.json() == {"terms": 1, "rules": 1}

    assert learning.list_terms(learning.INSTALLATION_SCOPE) == []
    assert len(learning.list_rules(99)) == 1


def test_deleting_an_account_goes_through_the_same_door():
    auth.enable("chef", "geheim1234")
    other = auth.create_user("zweiter", "geheim1234")
    learning.record_rule(other["id"], "cleanup", "Eigene Regel", 3)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO learned_terms (owner_id, language, source, target) "
            "VALUES (?, 'de', 'Jahwe', 'Yahweh')",
            (other["id"],),
        )

    auth.delete_user(other["id"])

    assert learning.list_terms(other["id"]) == []
    assert learning.list_rules(other["id"]) == []
