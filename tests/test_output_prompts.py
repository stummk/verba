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
    # not verbatim: only a type that may restructure reaches the output prompt
    type_row = project_types.create_type(
        "Custom", "Cleanup text.", "MY OUTPUT RULES.", verbatim=False
    )
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


# ── one document out of the answers to several chunks ─────────────────


def test_repeated_lists_and_restated_headings_are_merged():
    """A text too long for one call is structured chunk by chunk, and each
    chunk answers with the whole document's furniture."""
    answers = [
        [
            {"kind": "heading", "text": "Protokoll"},
            {"kind": "paragraph", "text": "Erster Teil."},
            {"kind": "list", "title": "Beschlüsse", "items": ["A", "B"]},
        ],
        [
            {"kind": "heading", "text": "protokoll "},  # restated, other spelling
            {"kind": "paragraph", "text": "Zweiter Teil."},
            {"kind": "list", "title": "Beschlüsse", "items": ["B", "C"]},
        ],
    ]

    merged = pdf._merge_document_blocks(answers)

    assert [b["kind"] for b in merged] == ["heading", "paragraph", "list", "paragraph"]
    assert merged[2]["items"] == ["A", "B", "C"]  # merged where the list opened
    assert [b["text"] for b in merged if b["kind"] == "paragraph"] == [
        "Erster Teil.",
        "Zweiter Teil.",
    ]


def test_a_heading_a_type_asks_for_repeatedly_is_kept():
    """Only a *restated* document heading goes: one that recurs inside an
    answer is the output prompt's own doing (one per question, say)."""
    answers = [
        [
            {"kind": "heading", "text": "Frage"},
            {"kind": "paragraph", "text": "Was war der Anlass?"},
            {"kind": "heading", "text": "Frage"},
            {"kind": "paragraph", "text": "Und danach?"},
        ],
        [
            {"kind": "heading", "text": "Frage"},  # opens the answer: dropped
            {"kind": "paragraph", "text": "Wer war dabei?"},
            {"kind": "heading", "text": "Frage"},  # inside it: kept
            {"kind": "paragraph", "text": "Wann?"},
        ],
    ]

    merged = pdf._merge_document_blocks(answers)

    assert sum(1 for b in merged if b["kind"] == "heading") == 3
    assert sum(1 for b in merged if b["kind"] == "paragraph") == 4


def test_a_long_text_still_yields_one_set_of_lists(monkeypatch):
    text = "\n\n".join(f"Absatz {i} " + "wort " * 300 for i in range(8))  # several chunks
    answer = (
        '[{"kind": "heading", "text": "Protokoll"}, '
        '{"kind": "paragraph", "text": "wort wort"}, '
        '{"kind": "list", "title": "To-dos", "items": ["etwas tun"]}]'
    )
    monkeypatch.setattr("verba.services.llm.chat", lambda messages, **kwargs: answer)

    blocks = pdf._structure_llm(text, "Instruction.", "", NO_CANCEL, no_report, (0, 100))

    assert sum(1 for b in blocks if b["kind"] == "heading") == 1
    assert sum(1 for b in blocks if b["kind"] == "list") == 1
    assert sum(1 for b in blocks if b["kind"] == "paragraph") > 1  # the text itself stays


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
