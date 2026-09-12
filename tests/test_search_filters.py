"""The tag filters of the search header: options, the narrowed overview, and
the same set reaching the hit list."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from verba import db
from verba.services import filters, vectorstore, workspace

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def make_file(project, name, **fields):
    """A file row straight in the database — no audio, no workspace needed."""
    with db.get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO files (project_id, filename, rel_path) VALUES (?, ?, ?)",
            (project["id"], name, f"audio/{name}"),
        )
        file_id = cursor.lastrowid
        for key, value in fields.items():
            conn.execute(
                f"UPDATE files SET {key} = ? WHERE id = ?",  # noqa: S608 — test-local keys
                (value, file_id),
            )
    return file_id


def add_segment(file_id, speaker, text="Hallo."):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker) "
            "VALUES (?, 0, 0.0, 1.0, ?, ?)",
            (file_id, text, speaker),
        )


@pytest.fixture
def material(tmp_path):
    from verba import config

    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    alpha = workspace.create_project("Alpha")
    beta = workspace.create_project("Beta")
    empty = workspace.create_project("Leer")
    german = make_file(alpha, "a.mp3", language="de", status="done", recorded_at="2024-01-05")
    make_file(alpha, "b.mp3", language="ru", status="pending", recorded_at="2024-02-05")
    english = make_file(beta, "c.mp3", language="en", status="pending", recorded_at="2025-06-01")
    add_segment(german, "Anna")
    add_segment(english, "Ben")
    return {"alpha": alpha, "beta": beta, "empty": empty, "german": german, "english": english}


# ── the options a picker offers ──────────────────────────────────────


def test_options_only_offer_what_actually_occurs(material):
    options = filters.options()
    assert [row["code"] for row in options["languages"]] == ["de", "en", "ru"]
    assert {row["status"] for row in options["statuses"]} == {"done", "pending"}
    assert [row["name"] for row in options["speakers"]] == ["Anna", "Ben"]
    assert options["date_first"] == "2024-01-05"
    assert options["date_last"] == "2025-06-01"


def test_a_file_without_its_own_date_falls_back_to_the_import_day(material):
    """A date range must not silently drop everything that was never tagged."""
    make_file(material["beta"], "d.mp3", recorded_at="")
    options = filters.options()
    # the import day is today, so it is the newest date there is
    assert options["date_last"] > "2025-06-01"


# ── the overview, narrowed ───────────────────────────────────────────


def names(file_filters):
    return [project["name"] for project in workspace.list_projects(None, file_filters)]


def test_without_a_filter_every_transcript_is_listed(material):
    assert set(names(None)) == {"Alpha", "Beta", "Leer"}
    assert set(names({})) == {"Alpha", "Beta", "Leer"}


def test_a_filter_keeps_only_transcripts_with_a_matching_file(material):
    assert names({"languages": ["de"]}) == ["Alpha"]
    assert names({"languages": ["en"]}) == ["Beta"]
    # several values in one field are an "either"
    assert set(names({"languages": ["de", "en"]})) == {"Alpha", "Beta"}
    # and a transcript without files falls out of every filtered overview
    assert "Leer" not in names({"statuses": ["pending"]})


def test_two_filters_are_an_and(material):
    assert names({"languages": ["de"], "statuses": ["done"]}) == ["Alpha"]
    assert names({"languages": ["de"], "statuses": ["pending"]}) == []


def test_the_speaker_filter_asks_the_segments(material):
    assert names({"speakers": ["Anna"]}) == ["Alpha"]
    assert names({"speakers": ["Ben"]}) == ["Beta"]
    assert set(names({"speakers": ["Anna", "Ben"]})) == {"Alpha", "Beta"}


def test_the_date_filter_is_a_range(material):
    assert names({"date_from": "2025-01-01"}) == ["Beta"]
    assert names({"date_to": "2024-12-31"}) == ["Alpha"]
    assert names({"date_from": "2024-02-01", "date_to": "2024-12-31"}) == ["Alpha"]


def test_a_filtered_card_counts_only_the_files_it_kept(material):
    """Otherwise a transcript claims twenty files where the filter found one."""
    [alpha] = workspace.list_projects(None, {"languages": ["de"]})
    assert alpha["file_count"] == 1
    assert alpha["done_count"] == 1
    unfiltered = next(p for p in workspace.list_projects() if p["name"] == "Alpha")
    assert unfiltered["file_count"] == 2


# ── the same set in the search ───────────────────────────────────────


def test_the_search_understands_the_same_filter_set(material):
    """`_filter_clause` and the overview share one builder, so a filter that
    empties the overview cannot leave hits standing in the list."""
    clause, params = vectorstore._filter_clause(
        {"languages": ["de", "en"], "statuses": ["done"]}, "c.speakers LIKE ?"
    )
    assert "f.language IN (?, ?)" in clause
    assert "f.status IN (?)" in clause
    assert params == ["de", "en", "done"]


def test_the_older_singular_spelling_still_reaches_the_same_sql(material):
    """The public API speaks of one language — a list of one is the same thing."""
    plural, plural_params = vectorstore._filter_clause({"languages": ["de"]}, "x LIKE ?")
    single, single_params = vectorstore._filter_clause({"language": "de"}, "x LIKE ?")
    assert plural == single and plural_params == single_params


def test_several_speakers_become_an_or(material):
    clause, params = vectorstore._filter_clause({"speakers": ["Anna", "Ben"]}, "c.speakers LIKE ?")
    assert clause.count("c.speakers LIKE ?") == 2
    assert " OR " in clause
    assert params == ["%Anna%", "%Ben%"]


# ── the routes ───────────────────────────────────────────────────────


def test_the_filter_options_route_is_not_swallowed_by_the_project_id_route(client, material):
    """`/api/projects/filters` has to be declared before `/{project_id}`."""
    response = client.get("/api/projects/filters")
    assert response.status_code == 200
    assert [row["code"] for row in response.json()["languages"]] == ["de", "en", "ru"]


def test_the_project_route_takes_the_filters_as_repeated_parameters(client, material):
    response = client.get("/api/projects", params=[("languages", "de"), ("languages", "en")])
    assert response.status_code == 200
    assert {row["name"] for row in response.json()} == {"Alpha", "Beta"}

    response = client.get("/api/projects", params={"date_from": "2025-01-01"})
    assert [row["name"] for row in response.json()] == ["Beta"]


# ── the tab is gone ──────────────────────────────────────────────────


def test_the_search_has_no_tab_of_its_own_any_more():
    """It lives in the transcript overview; an old link still lands there."""
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'data-route="search"' not in index
    assert "#/search" not in index

    app = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    assert "views/search.js" not in app
    assert 'MOVED_ROUTES = { search: "dashboard" }' in app
    assert not (FRONTEND / "js" / "views" / "search.js").exists()

    for lang in ("de", "en", "ru"):
        catalog = json.loads((FRONTEND / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "nav.search" not in catalog


def test_the_overview_carries_the_search_header():
    source = (FRONTEND / "js" / "views" / "dashboard.js").read_text(encoding="utf-8")
    assert "raw(headerMarkup())" in source
    assert 'mountSearchHeader(el("search-output"), refreshProjects, applySearchMode)' in source
    # a standing query owns the space: the cards are hidden, never thrown away
    assert "list.hidden = searchIsActive();" in source
    # and every list request carries the chips
    assert "api.listProjects(projectFilterQuery())" in source


def test_the_search_bar_carries_both_actions_and_a_way_back():
    source = (FRONTEND / "js" / "search-header.js").read_text(encoding="utf-8")
    assert 'decorate(runButton, "search"' in source
    assert 'decorate(askButton, "sparkle"' in source
    assert 'decorate(clearButton, "close"' in source
    # emptying the field puts the project cards back
    assert "if (!query && mode) {" in source
    # and a filter change re-runs whatever was asked
    assert "if (searchIsActive()) run(mode);" in source


def test_the_two_actions_sit_behind_the_field_and_are_dead_while_it_is_empty():
    """Magnifier and AI answer stand at the right end and only wake up with a
    query — an empty search is nothing either of them could run."""
    source = (FRONTEND / "js" / "search-header.js").read_text(encoding="utf-8")
    markup = source[source.index('search-bar" id="search-form') : source.index("filter-chips")]
    assert markup.index('id="search-query"') < markup.index('id="search-run"')
    assert markup.index('id="search-query"') < markup.index('id="search-ask"')
    # dead before the first keystroke, not only after one
    assert '<button type="submit" class="search-bar-btn" id="search-run" disabled>' in source
    assert "runButton.disabled = !query || running === runButton;" in source
    assert "askButton.disabled = !query || running === askButton;" in source
    # and every path that changes the query goes through the one place
    assert source.count("syncActions();") >= 4


def test_the_calendar_never_opens_on_a_date_that_does_not_parse():
    """An Invalid Date survives `??` and turns every cell of the grid to NaN."""
    source = (FRONTEND / "js" / "search-filters.js").read_text(encoding="utf-8")
    assert "return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null;" in source


def test_every_tag_filter_has_an_icon_and_a_label():
    source = (FRONTEND / "js" / "search-filters.js").read_text(encoding="utf-8")
    for key, icon, label in (
        ("type_ids", "category", "filters.type"),
        ("date", "dateRange", "filters.date"),
        # the language filter wears the icon the translation wears everywhere
        ("languages", "translate", "filters.language"),
        ("speakers", "people", "filters.speaker"),
        ("statuses", "checklist", "filters.status"),
    ):
        assert f'key: "{key}"' in source, key
        assert f'icon: "{icon}"' in source, icon
        assert f't("{label}")' in source, label
    # an active tag clears alone, and the row clears as a whole
    assert 'reset.className = "filter-chip-clear"' in source
    assert 't("filters.clearAll")' in source

    icons = (FRONTEND / "js" / "icons.js").read_text(encoding="utf-8")
    assert "  dateRange:" in icons

    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    # the header stays under the top bar while the list scrolls beneath it
    header = styles.split(".search-header {", 1)[1].split("}", 1)[0]
    assert "position: sticky" in header
    assert "top: var(--top-bar-h)" in header
    # the row scrolls sideways rather than wrapping onto a second line
    chips = styles.split(".filter-chips {", 1)[1].split("}", 1)[0]
    assert "overflow-x: auto" in chips


def test_the_date_filter_is_a_calendar_not_two_fields():
    """A range is two days and everything between them — that is drawn."""
    source = (FRONTEND / "js" / "search-filters.js").read_text(encoding="utf-8")
    assert 'id="filter-calendar"' in source
    assert 'type="date"' not in source, "the two native date fields are gone"
    # first click opens the range, second closes it, a later day cannot start it
    assert "if (!draftFrom || draftTo || iso < draftFrom) {" in source
    # and one day alone means that day, not "from here to forever"
    assert "filters.date_to = draftTo || draftFrom;" in source
    # the day under the pointer stands in for the end while only the start is set
    assert "function previewEnd()" in source
    # never toISOString(): it is UTC and moves the day across midnight
    assert "date.toISOString()" not in source
    # the week starts where the interface language starts it
    assert "function weekStart()" in source

    icons = (FRONTEND / "js" / "icons.js").read_text(encoding="utf-8")
    assert "  chevronLeft:" in icons and "  chevronRight:" in icons

    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    grid = styles.split(".daterange-grid {", 1)[1].split("}", 1)[0]
    assert "repeat(7, 1fr)" in grid
    assert "gap" not in grid, "the days of a range have to touch"
    for state in ("inside", "edge", "edge.start", "edge.end"):
        assert f".daterange-day.{state}" in styles, state


def test_the_service_worker_shell_knows_the_new_modules():
    source = (FRONTEND / "sw.js").read_text(encoding="utf-8")
    shell = set(re.findall(r'"([^"]+)"', re.search(r"const SHELL = \[(.*?)\];", source, re.S)[1]))
    assert {"/js/search-header.js", "/js/search-filters.js", "/js/search-results.js"} <= shell
    assert "/js/views/search.js" not in shell
