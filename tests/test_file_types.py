"""A transcript type per file: the project's is the rule, the file's the exception.

A project holds what was recorded, and that is not always of one kind — the
songs of a rehearsal next to the conversation about them. So the project's
type stays the global one and a single file may name its own, which then
decides everything about that file: how the cleanup runs, how the PDF is laid
out, and which type the search filters it under.
"""

from __future__ import annotations

import threading

import pytest

from verba import config, db
from verba.services import pdf, pipeline, project_types, workspace

NO_CANCEL = threading.Event()


@pytest.fixture(autouse=True)
def browse_root(tmp_path):
    settings = config.get_settings()
    settings.general.browse_roots = [str(tmp_path)]
    config.save_settings(settings)


def no_report(_percent: int, _message: str) -> None:
    pass


@pytest.fixture()
def data_env(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    project_types.seed_builtin_types()


def add_file(tmp_path, project, name: str, text: str = "Ein Satz."):
    source = tmp_path / name
    source.write_bytes(b"x")
    [row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(row["id"], "done")
    pipeline.save_text(row["id"], "cleanup", text)
    return workspace.get_file(row["id"])


def types_by_key() -> dict:
    return {entry["key"]: entry for entry in project_types.list_types()}


# ── the resolution itself ─────────────────────────────────────────────


def test_a_file_without_a_type_follows_its_project():
    project = {"type_id": 3, "type_key": "song", "type_name": "Song", "type_verbatim": 1}
    resolved = project_types.for_file(project, {"type_id": None, "type_name": None})
    assert resolved["type_name"] == "Song"
    assert resolved["type_id"] == 3


def test_a_files_own_type_wins_over_its_projects():
    project = {"type_id": 3, "type_key": "song", "type_name": "Song", "type_verbatim": 1}
    file_row = {"type_id": 7, "type_key": "protocol", "type_name": "Protokoll", "type_verbatim": 0}
    resolved = project_types.for_file(project, file_row)
    assert resolved["type_name"] == "Protokoll"
    assert project_types.is_verbatim(resolved) is False


def test_a_file_never_takes_half_of_each_type():
    """No field of the losing type may leak into the answer."""
    project = {"type_id": 3, "type_name": "Song", "type_output_prompt": "Strophen."}
    file_row = {"type_id": 7, "type_name": "Protokoll"}
    resolved = project_types.for_file(project, file_row)
    assert resolved["type_output_prompt"] is None


def test_without_a_project_and_without_a_file_everything_is_none():
    resolved = project_types.for_file(None, None)
    assert set(resolved) == set(project_types.TYPE_FIELDS)
    assert not any(resolved.values())
    # a transcript with no type at all reproduces its material
    assert project_types.is_verbatim(resolved) is True


# ── the API route ─────────────────────────────────────────────────────


def test_a_file_takes_a_type_of_its_own_and_gives_it_back(client, tmp_path):
    types = client.get("/api/types").json()
    song = next(entry for entry in types if entry["key"] == "song")
    protocol = next(entry for entry in types if entry["key"] == "protocol")
    project = client.post("/api/projects", json={"name": "P", "type_id": song["id"]}).json()
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    [row] = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(source)]}
    ).json()

    # it follows the project until it says otherwise
    assert row["type_id"] is None

    updated = client.put(f"/api/files/{row['id']}/type", json={"type_id": protocol["id"]}).json()
    assert updated["type_id"] == protocol["id"]
    assert updated["type_name"] == "Meeting Protocol"
    assert updated["type_verbatim"] == 0

    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["type_name"] == "Song"  # the project keeps its own
    assert detail["files"][0]["type_name"] == "Meeting Protocol"


def test_null_puts_a_file_back_under_its_project(client, tmp_path):
    types = client.get("/api/types").json()
    poem = next(entry for entry in types if entry["key"] == "poem")
    project = client.post("/api/projects", json={"name": "P"}).json()
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    [row] = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(source)]}
    ).json()

    client.put(f"/api/files/{row['id']}/type", json={"type_id": poem["id"]})
    back = client.put(f"/api/files/{row['id']}/type", json={"type_id": None}).json()
    assert back["type_id"] is None
    assert back["type_name"] is None


def test_an_unknown_type_is_refused(client, tmp_path):
    project = client.post("/api/projects", json={"name": "P"}).json()
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    [row] = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(source)]}
    ).json()
    assert client.put(f"/api/files/{row['id']}/type", json={"type_id": 9999}).status_code == 422


def test_deleting_a_type_leaves_the_file_with_its_project(client, tmp_path):
    types = client.get("/api/types").json()
    song = next(entry for entry in types if entry["key"] == "song")
    poem = next(entry for entry in types if entry["key"] == "poem")
    project = client.post("/api/projects", json={"name": "P", "type_id": song["id"]}).json()
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    [row] = client.post(
        f"/api/projects/{project['id']}/files/import", json={"paths": [str(source)]}
    ).json()
    client.put(f"/api/files/{row['id']}/type", json={"type_id": poem["id"]})

    client.delete(f"/api/types/{poem['id']}")
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["files"][0]["type_id"] is None
    assert detail["files"][0]["type_name"] is None


# ── the project overview ──────────────────────────────────────────────


def test_the_overview_names_every_type_a_project_transcribes_as(client, tmp_path):
    types = client.get("/api/types").json()
    song = next(entry for entry in types if entry["key"] == "song")
    protocol = next(entry for entry in types if entry["key"] == "protocol")
    project = client.post("/api/projects", json={"name": "P", "type_id": song["id"]}).json()
    for name in ("a.mp3", "b.mp3"):
        source = tmp_path / name
        source.write_bytes(b"x")
        client.post(f"/api/projects/{project['id']}/files/import", json={"paths": [str(source)]})
    files = client.get(f"/api/projects/{project['id']}").json()["files"]
    client.put(f"/api/files/{files[0]['id']}/type", json={"type_id": protocol["id"]})

    [listed] = client.get("/api/projects").json()
    assert listed["type_name"] == "Song"
    assert [entry["name"] for entry in listed["file_types"]] == ["Meeting Protocol"]


def test_a_project_whose_files_say_nothing_lists_no_extra_types(client, tmp_path):
    project = client.post("/api/projects", json={"name": "P"}).json()
    source = tmp_path / "a.mp3"
    source.write_bytes(b"x")
    client.post(f"/api/projects/{project['id']}/files/import", json={"paths": [str(source)]})
    [listed] = client.get("/api/projects").json()
    assert listed["file_types"] == []


# ── what the type actually decides ────────────────────────────────────


def with_segments(file_row):
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            [
                (file_row["id"], 0, 0.0, 2.0, "erster satz"),
                (file_row["id"], 1, 2.0, 4.0, "zweiter"),
            ],
        )
    return file_row


def echoing_chat(recorded: list):
    def chat(messages, model_override="", **kwargs):
        recorded.append(messages[0]["content"])
        return messages[-1]["content"]

    return chat


def test_the_cleanup_prompt_comes_from_the_files_own_type(data_env, tmp_path, monkeypatch):
    """The instruction the LLM is given belongs to the file, not to the project."""
    keys = types_by_key()
    project = workspace.create_project("P", keys["song"]["id"])
    file_row = with_segments(add_file(tmp_path, project, "a.mp3"))
    workspace.update_file(file_row["id"], {"type_id": keys["speech"]["id"]})

    seen: list[str] = []
    monkeypatch.setattr(pipeline.llm, "chat", echoing_chat(seen))
    pipeline.handle_llm_process_job(
        {"payload": {"file_id": file_row["id"], "steps": ["cleanup"]}}, NO_CANCEL, no_report
    )

    assert any("speech transcription" in prompt for prompt in seen)
    assert not any("Song transcription" in prompt for prompt in seen)


def test_whether_the_cleanup_reproduces_the_text_is_the_files_choice(
    data_env, tmp_path, monkeypatch
):
    """A minutes file in a song project must not be cleaned chunk by chunk."""
    keys = types_by_key()
    project = workspace.create_project("P", keys["song"]["id"])
    file_row = with_segments(add_file(tmp_path, project, "a.mp3"))
    workspace.update_file(file_row["id"], {"type_id": keys["protocol"]["id"]})

    seen: list[bool] = []

    def spy(
        file_id,
        type_prompt,
        model,
        cancel,
        report,
        progress_range=(0, 100),
        verbatim=True,
        whole=None,
    ):
        seen.append(verbatim)
        return "Text.", pipeline.overview.Overview()

    monkeypatch.setattr(pipeline, "run_cleanup", spy)
    pipeline.handle_llm_process_job(
        {"payload": {"file_id": file_row["id"], "steps": ["cleanup"]}}, NO_CANCEL, no_report
    )
    assert seen == [False]  # the protocol type, not the song the project names


def test_a_compilation_lays_out_every_file_by_its_own_type(data_env, tmp_path):
    """One PDF, two types — neither file may be rendered as the other one."""
    keys = types_by_key()
    project = workspace.create_project("P", keys["song"]["id"])
    first = add_file(tmp_path, project, "a.mp3", "Zeile eins\nZeile zwei")
    second = add_file(tmp_path, project, "b.mp3", "Ein Absatz.")
    workspace.update_file(second["id"], {"type_id": keys["speech"]["id"]})

    project = workspace.get_project(project["id"])
    docs = [
        pdf.build_document(workspace.get_file(row["id"]), project, "", NO_CANCEL, no_report)
        for row in (first, second)
    ]
    assert docs[0]["structure"] == "stanzas"  # the project's song type
    assert docs[1]["structure"] == "paragraphs"  # the file's own speech type


def test_the_page_break_is_decided_per_file(data_env, tmp_path):
    keys = types_by_key()
    project_types.update_type(
        keys["poem"]["id"],
        keys["poem"]["name"],
        keys["poem"]["system_prompt"],
        keys["poem"]["output_prompt"],
        keys["poem"]["structure"],
        keep_sections=True,
    )
    project = workspace.create_project("P", keys["song"]["id"])
    plain = add_file(tmp_path, project, "a.mp3")
    kept = add_file(tmp_path, project, "b.mp3")
    workspace.update_file(kept["id"], {"type_id": keys["poem"]["id"]})

    project = workspace.get_project(project["id"])
    docs = [
        pdf.build_document(workspace.get_file(row["id"]), project, "", NO_CANCEL, no_report)
        for row in (plain, kept)
    ]
    assert [doc["keep_sections"] for doc in docs] == [False, True]


def test_the_export_runs_through_with_mixed_types(data_env, tmp_path):
    keys = types_by_key()
    project = workspace.create_project("P", keys["song"]["id"])
    add_file(tmp_path, project, "a.mp3", "Zeile eins\nZeile zwei")
    second = add_file(tmp_path, project, "b.mp3", "Ein Absatz mit Text.")
    workspace.update_file(second["id"], {"type_id": keys["interview"]["id"]})

    name = pdf.handle_export_job(
        {"payload": {"scope": "project", "project_id": project["id"], "language": ""}},
        NO_CANCEL,
        no_report,
    )
    target = pdf.exports_dir(workspace.get_project(project["id"])) / name
    assert target.read_bytes().startswith(b"%PDF")
