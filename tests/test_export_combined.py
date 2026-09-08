"""Combined export: original plus translations in one PDF.

The export dialog offers either a single version (one PDF per language) or
everything in one document, with the versions of a file separated by a
centred "---" line.
"""

from __future__ import annotations

import threading
import time

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


def make_project(tmp_path, files: dict[str, dict[str, str]]):
    """files: name → {"": original, "en": translation, ...}"""
    types = {t["key"]: t for t in project_types.list_types()}
    project = workspace.create_project("P", types["song"]["id"])
    for name, versions in files.items():
        source = tmp_path / name
        source.write_bytes(b"x")
        [row] = workspace.import_paths(project, [str(source)])
        workspace.set_file_status(row["id"], "done")
        for language, text in versions.items():
            if language:
                pipeline.save_text(row["id"], "translation", text, language=language)
            else:
                pipeline.save_text(row["id"], "cleanup", text)
    return workspace.get_project(project["id"])


def run_export(payload):
    return pdf.handle_export_job({"payload": payload}, NO_CANCEL, no_report)


def rendered(monkeypatch) -> list:
    """Capture the docs handed to render_pdf plus the target path."""
    seen: list = []
    original = pdf.render_pdf

    def spy(docs, target, **kwargs):
        seen.append({"docs": docs, "target": target, **kwargs})
        return original(docs, target, **kwargs)

    monkeypatch.setattr(pdf, "render_pdf", spy)
    return seen


# ── which versions end up in the document ─────────────────────────────


def test_single_version_export_is_unchanged(data_env, tmp_path, monkeypatch):
    project = make_project(tmp_path, {"a.mp3": {"": "Original", "en": "Original in English"}})
    file_row = workspace.list_files(project["id"])[0]
    seen = rendered(monkeypatch)

    run_export({"scope": "file", "file_id": file_row["id"], "language": "en"})

    [call] = seen
    assert len(call["docs"]) == 1
    assert call["docs"][0]["blocks"] == [{"kind": "stanza", "lines": ["Original in English"]}]
    assert call["target"].name == "a.en.pdf"


def test_combined_export_appends_every_translation(data_env, tmp_path, monkeypatch):
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch", "en": "English", "ru": "Русский"}})
    file_row = workspace.list_files(project["id"])[0]
    seen = rendered(monkeypatch)

    run_export({"scope": "file", "file_id": file_row["id"], "combine": True})

    [call] = seen
    texts = [doc["blocks"][0]["lines"][0] for doc in call["docs"]]
    assert texts == ["Deutsch", "English", "Русский"]  # original first, then a-z
    assert call["target"].name == "a.all.pdf"


def test_combined_export_without_translations_is_just_the_original(data_env, tmp_path, monkeypatch):
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch"}})
    file_row = workspace.list_files(project["id"])[0]
    seen = rendered(monkeypatch)

    run_export({"scope": "file", "file_id": file_row["id"], "combine": True})

    [call] = seen
    assert len(call["docs"]) == 1
    assert not call["docs"][0].get("divider")


def test_appended_versions_carry_a_divider_and_no_second_header(data_env, tmp_path, monkeypatch):
    """The header would repeat identically — the divider is the separator."""
    project = make_project(tmp_path, {"20240817_Sommerlied.mp3": {"": "Deutsch", "en": "English"}})
    file_row = workspace.list_files(project["id"])[0]
    seen = rendered(monkeypatch)

    run_export({"scope": "file", "file_id": file_row["id"], "combine": True})

    original, translation = seen[0]["docs"]
    assert original["header_left"] == "Sommerlied"
    assert not original.get("divider")
    assert translation["header_left"] == ""
    assert translation["header_middle"] == ""
    assert translation["header_right"] == ""
    assert translation["divider"] is True


def test_combined_project_export_groups_versions_per_file(data_env, tmp_path, monkeypatch):
    project = make_project(
        tmp_path,
        {
            "20240817_Sommerlied.mp3": {"": "Sommer", "en": "Summer"},
            "20240901_Herbstlied.mp3": {"": "Herbst"},
        },
    )
    seen = rendered(monkeypatch)

    run_export({"scope": "project", "project_id": project["id"], "combine": True})

    docs = seen[0]["docs"]
    assert [doc["blocks"][0]["lines"][0] for doc in docs] == ["Sommer", "Summer", "Herbst"]
    # the divider marks a language switch, a header marks the next file
    assert [bool(doc.get("divider")) for doc in docs] == [False, True, False]
    assert [doc["header_left"] for doc in docs] == ["Sommerlied", "", "Herbstlied"]
    assert seen[0]["target"].name == "p.all.pdf"


def test_translation_languages_lists_only_stored_translations(data_env, tmp_path):
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch", "ru": "Русский", "en": "English"}})
    file_row = workspace.list_files(project["id"])[0]
    assert pdf.translation_languages(file_row["id"]) == ["en", "ru"]


# ── file names ────────────────────────────────────────────────────────


def test_export_names():
    assert pdf.export_name("lied", "") == "lied.pdf"
    assert pdf.export_name("lied", "en") == "lied.en.pdf"
    assert pdf.export_name("lied", "", combined=True) == "lied.all.pdf"
    # a combined export never overwrites a single-language one
    assert pdf.export_name("lied", "en", combined=True) == "lied.all.pdf"


# ── the divider on the page ───────────────────────────────────────────


def test_divider_is_a_centred_three_dash_line(data_env, tmp_path, monkeypatch):
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch", "en": "English"}})
    file_row = workspace.list_files(project["id"])[0]

    from fpdf import FPDF

    cells: list[tuple[str, str]] = []
    original = FPDF.cell

    def spy(self, w=0, h=0, text="", *args, **kwargs):
        if text:
            cells.append((kwargs.get("align", "L"), text))
        return original(self, w, h, text, *args, **kwargs)

    monkeypatch.setattr(FPDF, "cell", spy)
    run_export({"scope": "file", "file_id": file_row["id"], "combine": True})

    assert ("C", "---") in cells
    assert cells.count(("C", "---")) == 1  # exactly one, between the versions


def test_single_version_export_has_no_divider(data_env, tmp_path, monkeypatch):
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch", "en": "English"}})
    file_row = workspace.list_files(project["id"])[0]

    from fpdf import FPDF

    cells: list[str] = []
    original = FPDF.cell

    def spy(self, w=0, h=0, text="", *args, **kwargs):
        if text:
            cells.append(text)
        return original(self, w, h, text, *args, **kwargs)

    monkeypatch.setattr(FPDF, "cell", spy)
    run_export({"scope": "file", "file_id": file_row["id"], "language": "en"})

    assert "---" not in cells


def test_the_pdf_is_actually_written(data_env, tmp_path):
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch", "en": "English"}})
    file_row = workspace.list_files(project["id"])[0]

    run_export({"scope": "file", "file_id": file_row["id"], "combine": True})

    target = pdf.exports_dir(project) / "a.all.pdf"
    assert target.is_file() and target.stat().st_size > 0


def test_the_job_names_the_pdf_it_wrote(data_env, tmp_path):
    """The name is the job's answer, not something the client can derive: a
    combined export carries none of its languages, and a selection is named
    after its first file. Without it the editor could not offer the download.
    """
    project = make_project(
        tmp_path, {"a.mp3": {"": "Deutsch", "en": "English"}, "b.mp3": {"": "Zweite"}}
    )
    files = workspace.list_files(project["id"])

    assert run_export({"scope": "file", "file_id": files[0]["id"], "language": "en"}) == "a.en.pdf"
    assert run_export({"scope": "file", "file_id": files[0]["id"], "combine": True}) == "a.all.pdf"
    selection = run_export(
        {"scope": "project", "project_id": project["id"], "file_ids": [f["id"] for f in files]}
    )
    assert selection == "a+1.pdf"
    assert (pdf.exports_dir(project) / selection).is_file()


# ── API ───────────────────────────────────────────────────────────────


def test_the_editor_can_download_what_the_job_produced(client, tmp_path):
    """The path the editor walks: start the export, wait for the job the API
    handed back, and fetch the PDF under the name that job reports."""
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    project_types.seed_builtin_types()
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch", "en": "English"}})
    file_row = workspace.list_files(project["id"])[0]

    job = client.post(f"/api/files/{file_row['id']}/export", json={"language": "en"}).json()

    deadline = time.monotonic() + 30
    finished = None
    while time.monotonic() < deadline:
        rows = client.get("/api/jobs").json()
        current = next((row for row in rows if row["id"] == job["id"]), None)
        if current and current["status"] not in ("queued", "running"):
            finished = current
            break
        time.sleep(0.05)

    assert finished is not None, "export job did not finish"
    assert finished["status"] == "done", finished["error"]
    assert finished["result"] == "a.en.pdf"

    response = client.get(f"/api/projects/{project['id']}/exports/{finished['result']}")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_api_accepts_the_combine_flag(client, tmp_path, monkeypatch):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    project_types.seed_builtin_types()
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch", "en": "English"}})
    file_row = workspace.list_files(project["id"])[0]

    payloads: list[dict] = []
    monkeypatch.setattr(
        pdf.job_queue,
        "enqueue",
        lambda kind, payload, **kw: payloads.append(payload) or {"id": 1},
    )

    assert client.post(f"/api/files/{file_row['id']}/export", json={"combine": True}).status_code
    assert payloads[-1]["combine"] is True

    client.post(f"/api/projects/{project['id']}/export", json={"language": "en"})
    assert payloads[-1] == {
        "scope": "project",
        "project_id": project["id"],
        "file_ids": [],  # empty: the whole transcript, not a selection of it
        "language": "en",
        "combine": False,
    }


def test_the_types_page_break_reaches_the_renderer(data_env, tmp_path, monkeypatch):
    """The layout choice travels type → project → export, not by a prompt."""
    project = make_project(tmp_path, {"a.mp3": {"": "Deutsch"}, "b.mp3": {"": "Deutsch"}})
    seen = rendered(monkeypatch)

    run_export({"scope": "project", "project_id": project["id"]})
    assert all(entry["keep_sections"] is False for entry in seen[-1]["docs"])

    song = next(entry for entry in project_types.list_types() if entry["key"] == "song")
    project_types.update_type(
        song["id"],
        song["name"],
        song["system_prompt"],
        song["output_prompt"],
        song["structure"],
        keep_sections=True,
    )

    run_export({"scope": "project", "project_id": project["id"]})
    assert all(entry["keep_sections"] is True for entry in seen[-1]["docs"])
