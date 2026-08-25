"""Per-type output-format prompts for the PDF export.

The structure stage used to run one hardcoded prompt for every type. It is now
a type field: empty falls back to the default, new types are pre-filled with
it, and the builtins ship a tailored one.
"""

from __future__ import annotations

import threading

import pytest

from verba import config, db
from verba.services import pdf, project_types, workspace

NO_CANCEL = threading.Event()


def no_report(_percent: int, _message: str) -> None:
    pass


@pytest.fixture()
def data_env(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.llm.mode = "openai"
    settings.llm.base_url = "https://api.example.com/v1"
    settings.llm.model = "m"
    config.save_settings(settings)
    db.init_db()
    project_types.seed_builtin_types()


# ── the prompt that actually reaches the LLM ──────────────────────────


def test_type_output_prompt_replaces_the_default():
    prompt = pdf.output_system_prompt("Only paragraphs, nothing else.", "")
    assert prompt == "Only paragraphs, nothing else."
    assert "JSON array" not in prompt  # the type owns the whole instruction


def test_empty_output_prompt_falls_back_to_the_default():
    assert pdf.output_system_prompt("", "") == pdf.DEFAULT_OUTPUT_PROMPT
    assert pdf.output_system_prompt("   ", "") == pdf.DEFAULT_OUTPUT_PROMPT


def test_cleanup_prompt_is_appended_as_context():
    prompt = pdf.output_system_prompt("Format instruction.", "This is a song.")
    assert prompt.startswith("Format instruction.")
    assert prompt.endswith("This is a song.")


def test_default_prompt_carries_the_block_contract():
    assert pdf.BLOCK_CONTRACT in pdf.DEFAULT_OUTPUT_PROMPT
    for kind in pdf.BLOCK_KINDS:
        assert kind in pdf.DEFAULT_OUTPUT_PROMPT


def test_export_uses_the_type_output_prompt(data_env, tmp_path, monkeypatch):
    """End to end: what the type stores is what the structure stage sends."""
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    type_row = project_types.create_type("Custom", "Cleanup text.", "MY OUTPUT RULES.")
    project = workspace.create_project("P", type_row["id"])
    [file_row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(file_row["id"], "done")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker) "
            "VALUES (?, 0, 0, 1, 'Hallo Welt.', '')",
            (file_row["id"],),
        )

    sent: list[str] = []

    def fake_chat(messages, **kwargs):
        sent.append(messages[0]["content"])
        return '[{"kind": "paragraph", "text": "Hallo Welt."}]'

    monkeypatch.setattr("verba.services.llm.chat", fake_chat)
    pdf.build_document(
        workspace.get_file(file_row["id"]),
        workspace.get_project(project["id"]),
        "",
        NO_CANCEL,
        no_report,
    )

    assert sent, "the structure stage did not call the LLM"
    assert sent[0].startswith("MY OUTPUT RULES.")
    assert "Cleanup text." in sent[0]
    assert pdf.DEFAULT_OUTPUT_PROMPT not in sent[0]


# ── storage: defaults, pre-fill, restore ──────────────────────────────


def test_builtins_ship_a_tailored_output_prompt(client):
    types = client.get("/api/types").json()
    by_key = {entry["key"]: entry for entry in types}
    assert all(entry["output_prompt"] for entry in types)
    assert "stanza" in by_key["song"]["output_prompt"]
    assert "dialogue" in by_key["interview"]["output_prompt"]
    assert "To-dos" in by_key["protocol"]["output_prompt"]
    # the block contract is expanded into what is stored — nothing stays hidden
    assert pdf.BLOCK_CONTRACT in by_key["poem"]["output_prompt"]
    assert project_types.CONTRACT_PLACEHOLDER not in by_key["poem"]["output_prompt"]


def test_new_type_is_prefilled_with_the_default(client):
    created = client.post("/api/types", json={"name": "Podcast"}).json()
    assert created["output_prompt"] == pdf.DEFAULT_OUTPUT_PROMPT


def test_new_type_keeps_an_explicit_output_prompt(client):
    created = client.post(
        "/api/types",
        json={"name": "Podcast", "system_prompt": "c", "output_prompt": "Only paragraphs."},
    ).json()
    assert created["output_prompt"] == "Only paragraphs."


def test_emptying_the_output_prompt_is_allowed_and_uses_the_default(client):
    created = client.post("/api/types", json={"name": "Podcast"}).json()
    updated = client.put(
        f"/api/types/{created['id']}",
        json={"name": "Podcast", "system_prompt": "c", "output_prompt": ""},
    ).json()
    assert updated["output_prompt"] == ""  # stays empty, no silent re-fill
    assert pdf.output_system_prompt(updated["output_prompt"], "") == pdf.DEFAULT_OUTPUT_PROMPT


def test_both_prompts_are_updated_independently(client):
    created = client.post("/api/types", json={"name": "Podcast"}).json()
    updated = client.put(
        f"/api/types/{created['id']}",
        json={"name": "Podcast", "system_prompt": "New cleanup.", "output_prompt": "New output."},
    ).json()
    assert updated["system_prompt"] == "New cleanup."
    assert updated["output_prompt"] == "New output."


def test_default_prompts_endpoint_serves_the_prefill(client):
    body = client.get("/api/types/defaults").json()
    assert body["output_prompt"] == pdf.DEFAULT_OUTPUT_PROMPT


def test_restore_defaults_resets_an_edited_output_prompt(client):
    types = client.get("/api/types").json()
    song = next(entry for entry in types if entry["key"] == "song")
    client.put(
        f"/api/types/{song['id']}",
        json={"name": "Song", "system_prompt": "c", "output_prompt": "broken"},
    )
    client.post("/api/types/restore-defaults")
    restored = client.get("/api/types").json()
    assert "stanza" in next(e for e in restored if e["key"] == "song")["output_prompt"]


def test_project_detail_exposes_the_output_prompt(client):
    types = client.get("/api/types").json()
    song = next(entry for entry in types if entry["key"] == "song")
    project = client.post("/api/projects", json={"name": "S", "type_id": song["id"]}).json()
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert "stanza" in detail["type_output_prompt"]


# ── migration of an existing installation ─────────────────────────────


def test_seeded_installation_gets_the_output_prompts_backfilled(client):
    """Upgrade path: types seeded before output prompts existed."""
    with db.get_conn() as conn:
        conn.execute("UPDATE project_types SET output_prompt = ''")
        db.set_meta(conn, project_types.OUTPUT_PROMPT_MARKER, "")

    project_types.seed_builtin_types()  # what a restart would run

    types = project_types.list_types(include_legacy=False)
    assert all(entry["output_prompt"] for entry in types)


def test_backfill_never_overwrites_an_edited_prompt(client):
    types = client.get("/api/types").json()
    song = next(entry for entry in types if entry["key"] == "song")
    client.put(
        f"/api/types/{song['id']}",
        json={"name": "Song", "system_prompt": "c", "output_prompt": "mine"},
    )
    with db.get_conn() as conn:
        db.set_meta(conn, project_types.OUTPUT_PROMPT_MARKER, "")

    project_types.seed_builtin_types()

    keep = next(e for e in project_types.list_types() if e["key"] == "song")
    assert keep["output_prompt"] == "mine"
