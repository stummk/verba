"""Per-type layout (`structure`) for the PDF export.

Which text the export builds from, how it structures without an LLM and
whether character names are set in capitals used to be wired to fixed type
keys ("interview", "roleplay", "song", ...). It is now a field on the type, so
custom types can pick a layout too.
"""

from __future__ import annotations

import threading

import pytest

from verba import config, db
from verba.services import pdf, pipeline, project_types, workspace

NO_CANCEL = threading.Event()


def no_report(_percent: int, _message: str) -> None:
    pass


@pytest.fixture()
def data_env(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    project_types.seed_builtin_types()


def make_file(tmp_path, type_row, segments=(("Anna", "Hallo."), ("Ben", "Hi."))):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("P", type_row["id"])
    [file_row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(file_row["id"], "done")
    with db.get_conn() as conn:
        for idx, (speaker, text) in enumerate(segments):
            conn.execute(
                "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (file_row["id"], idx, idx, idx + 1, text, speaker),
            )
    return workspace.get_file(file_row["id"]), workspace.get_project(project["id"])


# ── validation ────────────────────────────────────────────────────────


def test_unknown_structure_falls_back_to_the_default():
    assert pdf.normalize_structure("nonsense") == pdf.DEFAULT_STRUCTURE
    assert pdf.normalize_structure(None) == pdf.DEFAULT_STRUCTURE
    assert pdf.normalize_structure("") == pdf.DEFAULT_STRUCTURE
    for value in pdf.STRUCTURES:
        assert pdf.normalize_structure(value) == value


def test_backfill_default_matches_the_export_default():
    """The backfill spells the default out; it must not drift from pdf.py."""
    untouched = {field: value for field, _marker, value in project_types._BACKFILL_FIELDS}
    assert untouched["structure"] == pdf.DEFAULT_STRUCTURE


def test_api_rejects_an_unknown_structure(client):
    response = client.post("/api/types", json={"name": "X", "structure": "freestyle"})
    assert response.status_code == 422


def test_defaults_endpoint_lists_the_choices(client):
    body = client.get("/api/types/defaults").json()
    assert body["structure"] == pdf.DEFAULT_STRUCTURE
    assert body["structures"] == list(pdf.STRUCTURES)


# ── builtins keep the behaviour the type keys used to give them ───────


def test_builtins_carry_the_layout_that_matches_them(client):
    by_key = {entry["key"]: entry for entry in client.get("/api/types").json()}
    assert by_key["song"]["structure"] == "stanzas"
    assert by_key["poem"]["structure"] == "stanzas"
    assert by_key["interview"]["structure"] == "dialogue"
    assert by_key["roleplay"]["structure"] == "script"
    assert by_key["speech"]["structure"] == "paragraphs"
    assert by_key["protocol"]["structure"] == "paragraphs"


def test_legacy_types_inherit_the_layout(client):
    """The German legacy keys mirror their modern counterpart."""
    by_key = {entry["key"]: entry for entry in project_types.list_types()}
    if "lied" in by_key:  # only seeded while all modern builtins exist
        assert by_key["lied"]["structure"] == "stanzas"
        assert by_key["rollenspiel"]["structure"] == "script"


# ── a custom type gets the same power ─────────────────────────────────


def test_custom_type_can_choose_dialogue(client, data_env, tmp_path):
    """The former blocker: a custom type could never use speaker segments."""
    custom = client.post("/api/types", json={"name": "Podcast", "structure": "dialogue"}).json()
    assert custom["structure"] == "dialogue"

    file_row, project = make_file(tmp_path, custom)
    pipeline.save_text(file_row["id"], "cleanup", "flattened text without speakers")
    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)
    assert doc["blocks"] == [
        {"kind": "dialogue", "speaker": "Anna", "text": "Hallo."},
        {"kind": "dialogue", "speaker": "Ben", "text": "Hi."},
    ]


def test_custom_type_can_choose_stanzas(client, data_env, tmp_path):
    custom = client.post("/api/types", json={"name": "Ballade", "structure": "stanzas"}).json()
    file_row, project = make_file(tmp_path, custom)
    pipeline.save_text(file_row["id"], "cleanup", "Zeile 1\nZeile 2\n\nZeile 3")
    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)
    assert doc["blocks"] == [
        {"kind": "stanza", "lines": ["Zeile 1", "Zeile 2"]},
        {"kind": "stanza", "lines": ["Zeile 3"]},
    ]


def test_paragraph_layout_uses_the_cleaned_text(client, data_env, tmp_path):
    custom = client.post("/api/types", json={"name": "Notiz", "structure": "paragraphs"}).json()
    file_row, project = make_file(tmp_path, custom)
    pipeline.save_text(file_row["id"], "cleanup", "Ein Absatz.\n\nEin zweiter.")
    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)
    assert doc["blocks"] == [
        {"kind": "paragraph", "text": "Ein Absatz."},
        {"kind": "paragraph", "text": "Ein zweiter."},
    ]


def test_structure_is_updatable(client):
    created = client.post("/api/types", json={"name": "Podcast"}).json()
    assert created["structure"] == "paragraphs"
    updated = client.put(
        f"/api/types/{created['id']}",
        json={"name": "Podcast", "structure": "script"},
    ).json()
    assert updated["structure"] == "script"


def test_project_detail_exposes_the_structure(client):
    types = client.get("/api/types").json()
    roleplay = next(entry for entry in types if entry["key"] == "roleplay")
    project = client.post("/api/projects", json={"name": "R", "type_id": roleplay["id"]}).json()
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["type_structure"] == "script"


# ── renderer ──────────────────────────────────────────────────────────


def test_script_layout_capitalizes_speakers(tmp_path):
    doc = {
        "title": "T",
        "date": "",
        "header_left": "",
        "header_middle": "",
        "header_right": "",
        "blocks": [{"kind": "dialogue", "speaker": "Anna", "text": "Hallo."}],
    }
    for structure in ("script", "dialogue"):
        target = tmp_path / f"{structure}.pdf"
        pdf.render_pdf([doc], structure, target)
        assert target.stat().st_size > 0
    # the capitals only differ inside the PDF; the deterministic renderer at
    # least has to produce different bytes for the two layouts
    assert (tmp_path / "script.pdf").read_bytes() != (tmp_path / "dialogue.pdf").read_bytes()


# ── migration of an existing installation ─────────────────────────────


def test_seeded_installation_gets_the_layouts_backfilled(client):
    """Upgrade path: types seeded before the layout field existed."""
    with db.get_conn() as conn:
        conn.execute("UPDATE project_types SET structure = 'paragraphs'")
        db.set_meta(conn, project_types.STRUCTURE_MARKER, "")

    project_types.seed_builtin_types()  # what a restart would run

    by_key = {entry["key"]: entry for entry in project_types.list_types()}
    assert by_key["song"]["structure"] == "stanzas"
    assert by_key["roleplay"]["structure"] == "script"


def test_backfill_runs_only_once(client):
    """After the backfill, a deliberate choice of the default has to survive."""
    types = client.get("/api/types").json()
    song = next(entry for entry in types if entry["key"] == "song")
    client.put(f"/api/types/{song['id']}", json={"name": "Song", "structure": "paragraphs"})

    project_types.seed_builtin_types()  # marker is set — no second backfill

    keep = next(e for e in project_types.list_types() if e["key"] == "song")
    assert keep["structure"] == "paragraphs"
