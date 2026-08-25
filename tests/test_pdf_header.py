"""The per-file PDF header: "title (addition)" left, date right.

Three fields per file, derived on import from the file name scheme
`date_source-target_title_addition` and editable in the editor. The renderer
puts them on one line: the title left (bold), the addition a single space
behind it in parentheses (regular), the date flush right (bold, as
dd.mm.yyyy). An empty field leaves no trace — no stray parentheses, no empty
header line.
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


def add_file(tmp_path, project, name: str):
    source = tmp_path / name
    source.write_bytes(b"x")
    [row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(row["id"], "done")
    pipeline.save_text(row["id"], "cleanup", f"Text von {name}")
    return workspace.get_file(row["id"])


def header_cells(monkeypatch, docs: list[dict], structure: str, target) -> list[tuple[str, str]]:
    """(alignment, text) of every cell the renderer writes — the header line
    is the only thing that uses cells; block text goes through multi_cell."""
    from fpdf import FPDF

    written: list[tuple[str, str]] = []
    original = FPDF.cell

    def spy(self, w=0, h=0, text="", *args, **kwargs):
        if text:
            written.append((kwargs.get("align", "L"), text))
        return original(self, w, h, text, *args, **kwargs)

    monkeypatch.setattr(FPDF, "cell", spy)
    pdf.render_pdf(docs, structure, target)
    return written


# ── derived on import ─────────────────────────────────────────────────


def test_full_filename_scheme_fills_all_three_fields(data_env, tmp_path):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "20240817_de_en_Sommerlied_Live in Rom.mp3")
    assert row["header_left"] == "Sommerlied"
    assert row["header_middle"] == "Live in Rom"
    assert row["header_right"] == "2024-08-17"


def test_short_filename_scheme_leaves_the_addition_empty(data_env, tmp_path):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "20240817_Sommerlied.mp3")
    assert row["header_left"] == "Sommerlied"
    assert row["header_middle"] == ""
    assert row["header_right"] == "2024-08-17"


def test_filename_without_a_date_still_gives_a_title(data_env, tmp_path):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "ohne datum.mp3")
    assert row["header_left"] == "ohne datum"
    assert row["header_right"] == ""


# ── editing and carrying through to the document ──────────────────────


def test_edited_header_reaches_the_document(data_env, tmp_path):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "a.mp3")
    workspace.update_file(
        row["id"],
        {
            "header_left": "Sommerlied",
            "header_middle": "Live in Rom",
            "header_right": "2024-08-17",
        },
    )
    doc = pdf.build_document(
        workspace.get_file(row["id"]),
        workspace.get_project(project["id"]),
        "",
        NO_CANCEL,
        no_report,
    )
    assert doc["header_left"] == "Sommerlied"
    assert doc["header_middle"] == "Live in Rom"
    assert doc["header_right"] == "17.08.2024"  # ISO date formatted for display


# ── the right-hand field is free text: place and/or date ──────────────


def test_a_place_next_to_the_date_survives(data_env, tmp_path):
    """A place plus an ISO date: the place stays, only the date is formatted."""
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "a.mp3")
    workspace.update_file(row["id"], {"header_right": "München, 2024-08-17"})
    doc = pdf.build_document(
        workspace.get_file(row["id"]),
        workspace.get_project(project["id"]),
        "",
        NO_CANCEL,
        no_report,
    )
    assert doc["header_right"] == "München, 17.08.2024"


def test_a_date_already_written_for_humans_is_left_alone(data_env, tmp_path):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "a.mp3")
    workspace.update_file(row["id"], {"header_right": "München, 28.01.1933"})
    doc = pdf.build_document(
        workspace.get_file(row["id"]),
        workspace.get_project(project["id"]),
        "",
        NO_CANCEL,
        no_report,
    )
    assert doc["header_right"] == "München, 28.01.1933"


def test_display_date_formatting_in_isolation():
    from verba.services.metadata import format_display_date

    assert format_display_date("2024-08-17") == "17.08.2024"
    assert format_display_date("München, 2024-08-17") == "München, 17.08.2024"
    assert format_display_date("28.01.1933") == "28.01.1933"
    assert format_display_date("") == ""
    assert format_display_date("2024-13-45") == "2024-13-45"  # not a real date


def test_a_place_without_a_date_is_passed_through(data_env, tmp_path):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "a.mp3")
    workspace.update_file(row["id"], {"header_right": "München"})
    doc = pdf.build_document(
        workspace.get_file(row["id"]),
        workspace.get_project(project["id"]),
        "",
        NO_CANCEL,
        no_report,
    )
    assert doc["header_right"] == "München"


# ── what lands on the page ────────────────────────────────────────────


def test_addition_follows_the_title_after_a_single_space(data_env, tmp_path, monkeypatch):
    """The addition belongs to the title, not into a column of its own."""
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "20240817_de_en_Sommerlied_Live in Rom.mp3")
    doc = pdf.build_document(row, workspace.get_project(project["id"]), "", NO_CANCEL, no_report)
    cells = header_cells(monkeypatch, [doc], "stanzas", tmp_path / "out.pdf")
    assert cells == [
        ("L", "Sommerlied"),
        ("L", " (Live in Rom)"),
        ("R", "17.08.2024"),
    ]


def test_an_addition_without_a_title_starts_at_the_margin(data_env, tmp_path, monkeypatch):
    """No leading space when there is no title in front of it."""
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "a.mp3")
    workspace.update_file(
        row["id"], {"header_left": "", "header_middle": "Live in Rom", "header_right": ""}
    )
    doc = pdf.build_document(
        workspace.get_file(row["id"]),
        workspace.get_project(project["id"]),
        "",
        NO_CANCEL,
        no_report,
    )
    cells = header_cells(monkeypatch, [doc], "stanzas", tmp_path / "out.pdf")
    assert cells == [("L", "(Live in Rom)")]


def test_empty_addition_leaves_no_parentheses(data_env, tmp_path, monkeypatch):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "20240817_Sommerlied.mp3")
    doc = pdf.build_document(row, workspace.get_project(project["id"]), "", NO_CANCEL, no_report)
    cells = header_cells(monkeypatch, [doc], "stanzas", tmp_path / "out.pdf")
    assert cells == [("L", "Sommerlied"), ("R", "17.08.2024")]


def test_folder_export_gives_every_file_its_own_header(data_env, tmp_path, monkeypatch):
    """The header is the only section marker in a project export, so each
    file has to bring its own."""
    project = workspace.create_project("P")
    rows = [
        add_file(tmp_path, project, "20240817_Sommerlied.mp3"),
        add_file(tmp_path, project, "20240901_de_en_Herbstlied_Studio.mp3"),
    ]
    project_row = workspace.get_project(project["id"])
    docs = [pdf.build_document(r, project_row, "", NO_CANCEL, no_report) for r in rows]

    cells = header_cells(monkeypatch, docs, "stanzas", tmp_path / "out.pdf")
    assert cells == [
        ("L", "Sommerlied"),
        ("R", "17.08.2024"),
        ("L", "Herbstlied"),
        ("L", " (Studio)"),
        ("R", "01.09.2024"),
    ]


def test_gaps_stay_tight(data_env, tmp_path, monkeypatch):
    """The vertical spacing is deliberately small — every gap the renderer
    inserts has to stay well under one text line (6 mm)."""
    project = workspace.create_project("P")
    rows = [
        add_file(tmp_path, project, "20240817_Sommerlied.mp3"),
        add_file(tmp_path, project, "20240901_Herbstlied.mp3"),
    ]
    project_row = workspace.get_project(project["id"])
    docs = [pdf.build_document(r, project_row, "", NO_CANCEL, no_report) for r in rows]

    from fpdf import FPDF

    gaps: list[float] = []
    original = FPDF.ln

    def spy(self, h=None):
        if h is not None:
            gaps.append(h)
        return original(self, h)

    monkeypatch.setattr(FPDF, "ln", spy)
    pdf.render_pdf(docs, "stanzas", tmp_path / "out.pdf")

    assert gaps, "the renderer inserted no spacing at all"
    assert max(gaps) <= pdf.GAP_BETWEEN_SECTIONS
    assert pdf.GAP_BETWEEN_SECTIONS < 6  # less than a line of text
    assert pdf.GAP_AFTER_HEADER <= 1.5  # header sits close to its text


def test_a_file_without_any_header_renders_no_header_line(data_env, tmp_path, monkeypatch):
    project = workspace.create_project("P")
    row = add_file(tmp_path, project, "a.mp3")
    workspace.update_file(row["id"], {"header_left": "", "header_middle": "", "header_right": ""})
    doc = pdf.build_document(
        workspace.get_file(row["id"]),
        workspace.get_project(project["id"]),
        "",
        NO_CANCEL,
        no_report,
    )
    assert header_cells(monkeypatch, [doc], "paragraphs", tmp_path / "out.pdf") == []
