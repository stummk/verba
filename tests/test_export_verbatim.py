"""The export reproduces the text — the reported failure.

A PDF came back with headings the text did not carry (although the type's
layout was running text) and with whole sentences of the cleaned version
missing: the structure stage had handed the text to the LLM, which rewrote it.
A transcript type is now `verbatim` by default, and then the structuring is
deterministic — nothing reaches the LLM that could reword, re-order or drop a
sentence. Only a type that deliberately turns its material into something else
(the minutes builtin) switches it off.
"""

from __future__ import annotations

import re
import threading

import pytest

from verba import config, db
from verba.services import pdf, pipeline, project_types, workspace

NO_CANCEL = threading.Event()

CLEANED = (
    "Сын Мой, не унывай, говорит Дух Святой. Я близок к твоей душе, говорит "
    "Господь, и Я вижу, в чем ты нуждаешься перед лицом Моим.\n\n"
    "Но тебе нужна вера Моя, говорит Господь. И с верой подходи. Но часто так "
    "сомневаешься во Мне.\n\n"
    "Ибо так тебе говорит Дух Святой, Аминь."
)

TRANSLATION = (
    "Mein Sohn, sei nicht verzagt, spricht der Heilige Geist.\n\n"
    "Aber du brauchst meinen Glauben, spricht der Herr.\n\n"
    "Denn so spricht der Heilige Geist zu dir, Amen."
)


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


def make_file(tmp_path, type_key="speech", text=CLEANED):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    with db.get_conn() as conn:
        row = conn.execute("SELECT id FROM project_types WHERE key = ?", (type_key,)).fetchone()
    project = workspace.create_project("P", row["id"])
    [file_row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(file_row["id"], "done")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker) "
            "VALUES (?, 0, 0, 1, 'Rohtext.', '')",
            (file_row["id"],),
        )
    pipeline.save_text(file_row["id"], "cleanup", text)
    return workspace.get_file(file_row["id"]), workspace.get_project(project["id"])


def explode(*_args, **_kwargs):
    raise AssertionError("the export must not ask the LLM about a verbatim type")


def words(value: str) -> list[str]:
    return re.findall(r"\w+", value.lower())


# ── nothing reaches the LLM ───────────────────────────────────────────


def test_verbatim_type_never_calls_the_llm(data_env, tmp_path, monkeypatch):
    file_row, project = make_file(tmp_path)
    monkeypatch.setattr("verba.services.llm.chat", explode)

    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)

    assert [block["kind"] for block in doc["blocks"]] == ["paragraph"] * 3


def test_verbatim_translation_never_calls_the_llm(data_env, tmp_path, monkeypatch):
    file_row, project = make_file(tmp_path)
    pipeline.save_text(file_row["id"], "translation", TRANSLATION, language="de")
    monkeypatch.setattr("verba.services.llm.chat", explode)

    doc = pdf.build_document(file_row, project, "de", NO_CANCEL, no_report)

    assert [block["text"] for block in doc["blocks"]] == TRANSLATION.split("\n\n")


# ── the text arrives whole ────────────────────────────────────────────


def test_every_word_of_the_cleaned_text_survives(data_env, tmp_path):
    file_row, project = make_file(tmp_path)

    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)

    # word for word and in order — no sentence dropped, none rephrased
    assert words(pdf.blocks_text(doc["blocks"])) == words(CLEANED)


def test_no_heading_is_invented_for_running_text(data_env, tmp_path):
    """What the report showed: speaker names promoted to bold headings."""
    file_row, project = make_file(tmp_path)

    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)

    assert all(block["kind"] == "paragraph" for block in doc["blocks"])


def test_paragraphs_keep_their_boundaries(data_env, tmp_path):
    file_row, project = make_file(tmp_path)

    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)

    assert [block["text"] for block in doc["blocks"]] == CLEANED.split("\n\n")


# ── deterministic structuring ─────────────────────────────────────────


def test_only_a_blank_line_starts_a_paragraph():
    """A single newline is where the text happened to wrap, not a break —
    `flow_text()` pulls it together at render time."""
    text = "Erste Zeile\nnoch dieselbe.\n\nNeuer Absatz.\nauch noch der."
    blocks = pdf._structure_rule_based(text, "paragraphs")

    assert blocks == [
        {"kind": "paragraph", "text": "Erste Zeile\nnoch dieselbe."},
        {"kind": "paragraph", "text": "Neuer Absatz.\nauch noch der."},
    ]


def test_text_without_a_blank_line_stays_one_paragraph():
    text = "Erste Zeile.\nZweite Zeile.\nDritte."
    assert pdf._structure_rule_based(text, "paragraphs") == [{"kind": "paragraph", "text": text}]


def test_an_empty_line_with_spaces_still_breaks():
    blocks = pdf._structure_rule_based("Absatz A.\n   \nAbsatz B.", "paragraphs")

    assert blocks == [
        {"kind": "paragraph", "text": "Absatz A."},
        {"kind": "paragraph", "text": "Absatz B."},
    ]


def test_rule_based_structuring_loses_nothing():
    for structure in pdf.STRUCTURES:
        blocks = pdf._structure_rule_based(CLEANED, structure)
        assert words(pdf.blocks_text(blocks)) == words(CLEANED), structure


# ── the switch ────────────────────────────────────────────────────────


def test_missing_field_counts_as_verbatim():
    """A project row from before the field existed keeps the promise."""
    assert pdf.is_verbatim({}) is True
    assert pdf.is_verbatim({"type_verbatim": 1}) is True
    assert pdf.is_verbatim({"type_verbatim": 0}) is False


def test_only_the_minutes_builtin_may_restructure(client):
    by_key = {entry["key"]: entry for entry in client.get("/api/types").json()}
    assert by_key["protocol"]["verbatim"] == 0
    for key in ("song", "poem", "interview", "roleplay", "speech"):
        assert by_key[key]["verbatim"] == 1, key


def test_a_new_type_is_verbatim(client):
    created = client.post("/api/types", json={"name": "Notiz"}).json()
    assert created["verbatim"] == 1
    assert client.get("/api/types/defaults").json()["verbatim"] is True


def test_the_switch_round_trips(client):
    created = client.post("/api/types", json={"name": "Bericht", "verbatim": False}).json()
    assert created["verbatim"] == 0
    updated = client.put(
        f"/api/types/{created['id']}", json={"name": "Bericht", "verbatim": True}
    ).json()
    assert updated["verbatim"] == 1


def test_project_detail_exposes_the_switch(client):
    types = client.get("/api/types").json()
    protocol = next(entry for entry in types if entry["key"] == "protocol")
    project = client.post("/api/projects", json={"name": "P", "type_id": protocol["id"]}).json()

    detail = client.get(f"/api/projects/{project['id']}").json()

    assert detail["type_verbatim"] == 0
    assert pdf.is_verbatim(detail) is False


# ── upgrading an existing installation ────────────────────────────────


def test_seeded_installation_gets_the_switch_backfilled(client):
    """Types seeded before the field existed: the schema default made every
    one of them verbatim, and the backfill hands the minutes type back."""
    with db.get_conn() as conn:
        conn.execute("UPDATE project_types SET verbatim = 1")
        db.set_meta(conn, project_types.VERBATIM_MARKER, "")

    project_types.seed_builtin_types()  # what a restart would run

    by_key = {entry["key"]: entry for entry in project_types.list_types()}
    assert by_key["protocol"]["verbatim"] == 0
    assert by_key["speech"]["verbatim"] == 1


def test_backfill_leaves_a_deliberate_choice_alone(client):
    types = client.get("/api/types").json()
    speech = next(entry for entry in types if entry["key"] == "speech")
    client.put(f"/api/types/{speech['id']}", json={"name": "Speech", "verbatim": False})

    project_types.seed_builtin_types()  # marker is set — no second backfill

    keep = next(e for e in project_types.list_types() if e["key"] == "speech")
    assert keep["verbatim"] == 0
