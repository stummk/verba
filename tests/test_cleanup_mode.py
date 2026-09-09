"""How the aufbereitung runs, and what the export may do, are two questions.

The reported failure: a type with "the export reproduces the text unchanged"
did not seem to be processed at all — the aufbereitung handed back the
transcription almost word for word. It did run, but on the only path that
promise used to allow: cleaning section by section, where the type's own
prompt is merely context about the material. Whoever wanted the recording
rewritten had to switch the promise off — and thereby let the export rewrite
it too.

So the type carries two switches now: `verbatim` for the export
(`pdf.build_document`) and `condense` for the aufbereitung
(`pipeline.run_cleanup`). Any combination of them is a valid choice, and the
one this file cares about most is a document written by the LLM that reaches
the PDF word for word.
"""

from __future__ import annotations

import json
import threading

import pytest

from verba import config, db
from verba.services import overview, pdf, pipeline, project_types, workspace

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


class Recorder:
    """Fake LLM that answers every kind of call and remembers what it got."""

    KINDS = {
        "You condense one section": "digest",
        "You merge the digests": "fold",
        "You state what one recording": "overview",
        "You write one document": "document",
        "You clean up automatic": "cleanup",
        "You lay out": "structure",
    }

    def __init__(self, **answers: str) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.answers = answers

    def __call__(self, messages, model_override="", **kwargs):
        system, user = messages[0]["content"], messages[-1]["content"]
        kind = next((k for p, k in self.KINDS.items() if system.startswith(p)), "other")
        self.calls.append((kind, system, user))
        if kind in self.answers:
            return self.answers[kind]
        if kind == "overview":
            return json.dumps({"title": "Die Probe", "summary": "Kurz.", "terms": ["Wesner"]})
        if kind == "document":
            return "Der Bericht\n\nEs wurde geprobt und beschlossen."
        return user  # digest, fold, cleanup: echo the material


def make_type(*, verbatim: bool, condense: bool) -> dict:
    return project_types.create_type(
        "Bericht",
        "Schreibe aus der Aufnahme einen Bericht.",
        "",
        "paragraphs",
        False,
        verbatim,
        condense,
    )


def make_file(tmp_path, type_row: dict, segments: int = 3):
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("P", type_row["id"])
    [file_row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(file_row["id"], "done")
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            [
                (file_row["id"], i, i * 30.0, (i + 1) * 30.0, f"Abschnitt {i} " + wording(i))
                for i in range(segments)
            ],
        )
    return workspace.get_file(file_row["id"]), workspace.get_project(project["id"])


def wording(index: int) -> str:
    """Enough distinct words for the guard against a summarized section."""
    return " ".join(f"begriff{index}n{n}" for n in range(60))


def run_cleanup_job(file_id: int) -> None:
    job = {"payload": {"file_id": file_id, "steps": ["cleanup"]}}
    pipeline.handle_llm_process_job(job, NO_CANCEL, no_report)


# ── the combination that was missing ─────────────────────────────────


def test_a_verbatim_type_may_still_have_its_recording_rewritten(tmp_path, monkeypatch):
    """The reported wish: the aufbereitung works, the export keeps its result."""
    type_row = make_type(verbatim=True, condense=True)
    file_row, project = make_file(tmp_path, type_row)
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run_cleanup_job(file_row["id"])

    # the type's prompt ran once over the whole recording, not per chunk
    assert [call for call in llm.calls if call[0] == "document"]
    assert not [call for call in llm.calls if call[0] == "cleanup"]
    stored = pipeline.get_text(file_row["id"], "cleanup")["content"]
    assert stored.startswith("Der Bericht")

    # and the export hands that document on unchanged, without asking an LLM
    def explode(*_args, **_kwargs):
        raise AssertionError("a verbatim type must not reach the LLM at export")

    monkeypatch.setattr("verba.services.llm.chat", explode)
    doc = pdf.build_document(workspace.get_file(file_row["id"]), project, "", NO_CANCEL, no_report)
    assert [block["text"] for block in doc["blocks"]] == stored.split("\n\n")


def test_the_export_switch_does_not_decide_how_the_cleanup_runs(tmp_path, monkeypatch):
    """A type that reproduces its material is cleaned chunk by chunk — even
    when the export is the one allowed to restructure."""
    type_row = make_type(verbatim=False, condense=False)
    file_row, _project = make_file(tmp_path, type_row)
    llm = Recorder()
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run_cleanup_job(file_row["id"])

    assert [call for call in llm.calls if call[0] == "cleanup"]
    assert not [call for call in llm.calls if call[0] == "document"]
    stored = pipeline.get_text(file_row["id"], "cleanup")["content"]
    for index in range(3):
        assert f"Abschnitt {index} " in stored


def test_a_condensing_type_is_never_accused_of_summarizing(tmp_path, monkeypatch):
    """The guard belongs to the reproducing path, not to the export choice."""
    type_row = make_type(verbatim=True, condense=True)
    file_row, _project = make_file(tmp_path, type_row)
    llm = Recorder(document="Kurz: es wurde geprobt.")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    run_cleanup_job(file_row["id"])

    assert pipeline.get_text(file_row["id"], "cleanup")["content"].startswith("Kurz:")


def test_a_reproducing_type_still_refuses_a_summary(tmp_path, monkeypatch):
    type_row = make_type(verbatim=True, condense=False)
    file_row, _project = make_file(tmp_path, type_row)
    llm = Recorder(cleanup="Kurz: es wurde geprobt.")
    monkeypatch.setattr(pipeline.llm, "chat", llm)
    monkeypatch.setattr(overview.llm, "chat", llm)

    with pytest.raises(RuntimeError, match="zusammengefasst"):
        run_cleanup_job(file_row["id"])

    assert pipeline.get_text(file_row["id"], "cleanup") is None


# ── the field itself ─────────────────────────────────────────────────


def test_a_row_without_the_field_reproduces_its_material():
    assert project_types.condenses({}) is False
    assert project_types.condenses({"type_condense": None}) is False
    assert project_types.condenses({"type_condense": 1}) is True


def test_only_the_minutes_builtin_writes_its_own_document():
    by_key = {entry["key"]: entry for entry in project_types.list_types()}
    assert by_key["protocol"]["condense"] == 1
    for key in ("song", "interview", "speech", "poem", "roleplay"):
        assert by_key[key]["condense"] == 0, key


def test_the_api_carries_the_choice_both_ways(client):
    created = client.post(
        "/api/types", json={"name": "Bericht", "verbatim": True, "condense": True}
    ).json()
    assert created["condense"] == 1 and created["verbatim"] == 1
    assert client.get("/api/types/defaults").json()["condense"] is False

    updated = client.put(
        f"/api/types/{created['id']}", json={"name": "Bericht", "condense": False}
    ).json()
    assert updated["condense"] == 0


def test_an_older_installation_keeps_the_way_its_types_worked():
    """`verbatim` used to decide both, for custom types as well as builtins.
    A database written back then must not silently start cleaning its minutes
    section by section."""
    own = make_type(verbatim=False, condense=False)  # what the old form stored
    with db.get_conn() as conn:
        conn.execute("DELETE FROM meta WHERE key = 'project_types_condense_from_verbatim'")

    db.init_db()  # what a restart runs

    by_key = {entry["key"]: entry for entry in project_types.list_types()}
    assert by_key[own["key"]]["condense"] == 1  # it wrote its own document, still does
    assert by_key["song"]["condense"] == 0
