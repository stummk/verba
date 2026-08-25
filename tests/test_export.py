"""PDF export: structuring (rule-based + LLM), deterministic renderer, API."""

from __future__ import annotations

import threading
import time

import pytest

from verba import config, db
from verba.services import pdf, pipeline, workspace
from verba.services.project_types import seed_builtin_types

NO_CANCEL = threading.Event()


def no_report(_percent: int, _message: str) -> None:
    pass


@pytest.fixture()
def data_env(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    seed_builtin_types()


def make_done_file(tmp_path, name="a.mp3", type_key=None, segments=(("", "Hallo Welt."),)):
    source = tmp_path / name
    source.write_bytes(b"x")
    type_id = None
    if type_key:
        with db.get_conn() as conn:
            row = conn.execute("SELECT id FROM project_types WHERE key = ?", (type_key,)).fetchone()
        type_id = row["id"]
    project = workspace.create_project(f"P-{name}", type_id)
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


# ── block parsing (LLM answer → validated structure) ─────────────────


def test_parse_blocks_accepts_json_with_surrounding_prose():
    raw = 'Hier: [{"kind": "paragraph", "text": "Hallo"}, {"kind": "separator"}] fertig.'
    assert pdf._parse_blocks(raw) == [
        {"kind": "paragraph", "text": "Hallo"},
        {"kind": "separator"},
    ]


def test_parse_blocks_filters_unknown_kinds_and_empty_texts():
    raw = (
        '[{"kind": "magic", "text": "x"}, {"kind": "paragraph", "text": "  "}, '
        '{"kind": "heading", "text": "T"}]'
    )
    assert pdf._parse_blocks(raw) == [{"kind": "heading", "text": "T"}]


def test_parse_blocks_rejects_garbage():
    assert pdf._parse_blocks("kein json") is None
    assert pdf._parse_blocks('{"kind": "paragraph"}') is None


# ── rule-based structuring ────────────────────────────────────────────


def test_rule_based_paragraphs():
    blocks = pdf._structure_rule_based("Absatz eins.\n\nAbsatz zwei.", "")
    assert blocks == [
        {"kind": "paragraph", "text": "Absatz eins."},
        {"kind": "paragraph", "text": "Absatz zwei."},
    ]


def test_rule_based_stanzas_for_poems():
    blocks = pdf._structure_rule_based("Zeile 1\nZeile 2\n\nZeile 3", "stanzas")
    assert blocks[0] == {"kind": "stanza", "lines": ["Zeile 1", "Zeile 2"]}
    assert blocks[1] == {"kind": "stanza", "lines": ["Zeile 3"]}


def test_rule_based_dialogue_from_speaker_lines():
    blocks = pdf._structure_rule_based("Anna: Hallo.\nOhne Sprecher.", "dialogue")
    assert blocks[0] == {"kind": "dialogue", "speaker": "Anna", "text": "Hallo."}
    assert blocks[1] == {"kind": "paragraph", "text": "Ohne Sprecher."}


# ── base text selection ───────────────────────────────────────────────


def test_base_text_prefers_cleanup(data_env, tmp_path):
    file_row, _project = make_done_file(tmp_path)
    pipeline.save_text(file_row["id"], "cleanup", "Bereinigt.")
    assert pdf._base_text(file_row["id"], "", "") == "Bereinigt."


def test_base_text_dialogue_structures_use_segment_speakers(data_env, tmp_path):
    file_row, _project = make_done_file(
        tmp_path, type_key="interview", segments=(("Anna", "Hallo."), ("Ben", "Hi."))
    )
    pipeline.save_text(file_row["id"], "cleanup", "ohne sprecher")
    assert pdf._base_text(file_row["id"], "dialogue", "") == "Anna: Hallo.\nBen: Hi."
    assert pdf._base_text(file_row["id"], "script", "") == "Anna: Hallo.\nBen: Hi."
    # every other structure works from the cleaned-up text
    assert pdf._base_text(file_row["id"], "paragraphs", "") == "ohne sprecher"


def test_base_text_missing_translation_raises(data_env, tmp_path):
    file_row, _project = make_done_file(tmp_path)
    with pytest.raises(RuntimeError, match="translation"):
        pdf._base_text(file_row["id"], "", "en")


# ── stage 1 with LLM (fake chat) ──────────────────────────────────────


def configure_llm():
    settings = config.get_settings()
    settings.llm.mode = "openai"
    settings.llm.base_url = "https://api.example.com/v1"
    settings.llm.model = "m"
    config.save_settings(settings)


def test_build_document_uses_llm_blocks(data_env, tmp_path, monkeypatch):
    configure_llm()
    file_row, project = make_done_file(tmp_path, type_key="speech")
    monkeypatch.setattr(
        "verba.services.llm.chat",
        lambda messages, **kw: '[{"kind": "heading", "text": "Speech"}]',
    )
    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)
    assert doc["blocks"] == [{"kind": "heading", "text": "Speech"}]


def test_build_document_falls_back_on_bad_llm_answer(data_env, tmp_path, monkeypatch):
    configure_llm()
    file_row, project = make_done_file(tmp_path, type_key="speech")
    monkeypatch.setattr("verba.services.llm.chat", lambda messages, **kw: "kein json")
    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)
    assert doc["blocks"] == [{"kind": "paragraph", "text": "Hallo Welt."}]


def test_build_document_without_type_never_calls_llm(data_env, tmp_path, monkeypatch):
    configure_llm()
    file_row, project = make_done_file(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("LLM darf ohne Typ nicht aufgerufen werden")

    monkeypatch.setattr("verba.services.llm.chat", boom)
    doc = pdf.build_document(file_row, project, "", NO_CANCEL, no_report)
    assert doc["blocks"] == [{"kind": "paragraph", "text": "Hallo Welt."}]


def test_build_document_includes_file_header(data_env, tmp_path):
    file_row, project = make_done_file(tmp_path)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE files SET header_left = 'Text', header_middle = 'Zusatz', "
            "header_right = 'Prefix' WHERE id = ?",
            (file_row["id"],),
        )
    doc = pdf.build_document(workspace.get_file(file_row["id"]), project, "", NO_CANCEL, no_report)
    assert doc["header_left"] == "Text"
    assert doc["header_middle"] == "Zusatz"
    assert doc["header_right"] == "Prefix"


def test_import_populates_automatic_header_defaults(data_env, tmp_path):
    file_row, _project = make_done_file(tmp_path, name="aufnahme.mp3")
    assert file_row["header_left"] == "aufnahme"
    assert file_row["header_middle"] == ""
    assert file_row["header_right"] == ""


# ── stage 2: renderer ─────────────────────────────────────────────────

ALL_KINDS_DOC = {
    "title": "Titel",
    "date": "2024-08-17",
    "blocks": [
        {"kind": "heading", "text": "Kapitel"},
        {"kind": "paragraph", "text": "Ein Absatz mit Umlauten: äöü und Кириллица."},
        {"kind": "stanza", "lines": ["Vers eins", "Vers zwei"]},
        {"kind": "dialogue", "speaker": "Anna", "text": "Hallo."},
        {"kind": "list", "title": "To-dos", "items": ["eins", "zwei"]},
        {"kind": "separator"},
        {"kind": "paragraph", "text": "Schluss."},
    ],
}


@pytest.mark.parametrize("structure", ["", "paragraphs", "stanzas", "dialogue", "script"])
def test_render_pdf_all_block_kinds(tmp_path, structure):
    target = tmp_path / "out.pdf"
    pdf.render_pdf([ALL_KINDS_DOC], structure, target)
    assert target.read_bytes().startswith(b"%PDF")


def test_render_pdf_folder_sections_without_toc(tmp_path):
    """Folder export: sections flow with spacing only — one continuous PDF."""
    docs = [dict(ALL_KINDS_DOC, title=f"Datei {i}") for i in range(3)]
    target = tmp_path / "sammel.pdf"
    pdf.render_pdf(docs, "speech", target)
    data = target.read_bytes()
    assert data.startswith(b"%PDF")


# ── job handler & API ─────────────────────────────────────────────────


def wait_for_job(client, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
        job = jobs.get(job_id)
        if job and job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("Job wurde nicht fertig")


def test_export_endpoints_end_to_end(client, tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)

    file_row, project = make_done_file(tmp_path, segments=(("", "Erster Satz."),))
    job = client.post(f"/api/files/{file_row['id']}/export", json={}).json()
    finished = wait_for_job(client, job["id"])
    assert finished["status"] == "done", finished

    exports = client.get(f"/api/projects/{project['id']}/exports").json()
    assert [e["name"] for e in exports] == ["a.pdf"]

    download = client.get(f"/api/projects/{project['id']}/exports/a.pdf")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")

    assert client.delete(f"/api/projects/{project['id']}/exports/a.pdf").json()["deleted"]
    assert client.get(f"/api/projects/{project['id']}/exports").json() == []


def test_project_export_collects_done_files(client, tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)

    file_row, project = make_done_file(tmp_path, name="one.mp3")
    job = client.post(f"/api/projects/{project['id']}/export", json={}).json()
    finished = wait_for_job(client, job["id"])
    assert finished["status"] == "done", finished
    exports = client.get(f"/api/projects/{project['id']}/exports").json()
    assert exports[0]["name"] == f"{project['slug']}.pdf"


def test_project_export_requires_done_files(client, tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    project = workspace.create_project("Leer")
    response = client.post(f"/api/projects/{project['id']}/export", json={})
    assert response.status_code == 422


def test_export_download_rejects_traversal(client, tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    project = workspace.create_project("Sicher")
    response = client.get(f"/api/projects/{project['id']}/exports/..%2Fproject.json")
    assert response.status_code in (403, 404)
