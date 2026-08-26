"""Changing the workspaces directory takes the existing projects with it.

Files are tracked relative to their project folder, so a move is: move the
folder, rewrite the one absolute path per project. These tests pin down the
absolute-path handling (Windows drive letters, quotes, ~), the collision
refusal and that nothing is left behind.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from verba import config, db
from verba.services import project_types, workspace

NO_CANCEL = threading.Event()


def no_report(_percent: int, _message: str) -> None:
    pass


@pytest.fixture()
def env(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "old")
    config.save_settings(settings)
    db.init_db()
    project_types.seed_builtin_types()
    return tmp_path


def make_project(name="Lieder", files=("a.mp3",)):
    project = workspace.create_project(name)
    for filename in files:
        target = workspace.project_dir(project) / "audio" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio")
    return workspace.get_project(project["id"])


# ── absolute paths ────────────────────────────────────────────────────


def test_a_relative_path_is_stored_absolute():
    """Otherwise it would point at whatever the working directory happens to be."""
    settings = config.Settings.model_validate({"general": {"workspaces_dir": "workspaces"}})
    stored = settings.general.workspaces_dir
    assert stored != "workspaces"
    assert Path(stored).is_absolute()


def test_quotes_and_variables_are_expanded(monkeypatch):
    monkeypatch.setenv("VERBA_TEST_ROOT", str(Path.home() / "verba-root"))
    quoted = '"%VERBA_TEST_ROOT%"'  # the Windows explorer copies paths with quotes
    settings = config.Settings.model_validate({"general": {"workspaces_dir": quoted}})
    assert settings.general.workspaces_dir == str(Path.home() / "verba-root")


def test_an_empty_path_keeps_the_default():
    settings = config.Settings()
    assert settings.general.workspaces_dir == ""
    assert config.workspaces_root(settings) == config.default_workspaces_dir()


# ── moving ────────────────────────────────────────────────────────────


def test_the_project_folder_moves_and_the_database_follows(env):
    project = make_project()
    old_dir = workspace.project_dir(project)
    new_root = env / "new"

    moved = workspace.move_workspaces(new_root)

    assert moved == 1
    assert not old_dir.exists()
    fresh = workspace.get_project(project["id"])
    assert workspace.project_dir(fresh) == new_root / project["slug"]
    assert (new_root / project["slug"] / "audio" / "a.mp3").read_bytes() == b"audio"


def test_file_paths_resolve_after_the_move(env):
    project = make_project(files=("a.mp3",))
    source = env / "import.mp3"
    source.write_bytes(b"x")
    [file_row] = workspace.import_paths(project, [str(source)])

    workspace.move_workspaces(env / "new")

    path = workspace.file_path(workspace.get_file(file_row["id"]))
    assert path.is_file()
    assert path.parent.parent == env / "new" / project["slug"]


def test_every_project_moves(env):
    first = make_project("Erstes")
    second = make_project("Zweites")

    workspace.move_workspaces(env / "new")

    for project in (first, second):
        fresh = workspace.get_project(project["id"])
        assert workspace.project_dir(fresh).parent == env / "new"
        assert workspace.project_dir(fresh).is_dir()


def test_a_name_collision_is_refused_before_anything_moves(env):
    project = make_project()
    new_root = env / "new"
    (new_root / project["slug"]).mkdir(parents=True)
    (new_root / project["slug"] / "fremd.txt").write_text("nicht von uns", encoding="utf-8")

    with pytest.raises(RuntimeError, match="gleichem Namen"):
        workspace.move_workspaces(new_root)

    assert workspace.project_dir(project).is_dir()  # the original is untouched


def test_an_empty_folder_in_the_target_is_no_obstacle(env):
    project = make_project()
    new_root = env / "new"
    (new_root / project["slug"]).mkdir(parents=True)

    workspace.move_workspaces(new_root)

    assert (new_root / project["slug"] / "audio" / "a.mp3").is_file()


def test_a_missing_folder_is_recreated_instead_of_failing(env):
    project = make_project()
    shutil.rmtree(workspace.project_dir(project))

    workspace.move_workspaces(env / "new")

    fresh = workspace.get_project(project["id"])
    assert (workspace.project_dir(fresh) / "audio").is_dir()


def test_moving_to_the_same_root_does_nothing(env):
    make_project()
    assert workspace.move_workspaces(env / "old") == 0


def test_the_job_handler_moves_to_the_payload_root(env):
    project = make_project()
    new_root = env / "new"

    workspace.handle_move_workspace_job({"payload": {"root": str(new_root)}}, NO_CANCEL, no_report)

    assert workspace.project_dir(workspace.get_project(project["id"])).parent == new_root


def test_progress_is_reported_per_project(env):
    make_project("Erstes")
    make_project("Zweites")
    seen: list[tuple[int, str]] = []

    workspace.move_workspaces(env / "new", NO_CANCEL, lambda p, m: seen.append((p, m)))

    assert seen[-1][0] == 100
    assert any("Erstes".lower() in message.lower() for _, message in seen)


# ── new projects and the API ──────────────────────────────────────────


def test_new_projects_land_in_the_new_root(env):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(env / "new")
    config.save_settings(settings)

    project = workspace.create_project("Danach")

    assert workspace.project_dir(project).parent == env / "new"


def test_the_settings_endpoint_enqueues_the_move(client, env, monkeypatch):
    project = make_project()
    payloads: list[dict] = []
    from verba.api import settings as settings_api

    monkeypatch.setattr(
        settings_api.job_queue,
        "enqueue",
        lambda kind, payload, **kw: payloads.append({"kind": kind, **payload}) or {"id": 1},
    )

    body = client.get("/api/settings").json()
    body["general"]["workspaces_dir"] = str(env / "new")
    response = client.put("/api/settings", json=body)

    assert response.status_code == 200
    assert response.json()["workspace_move"] == {"root": str(env / "new"), "projects": 1}
    assert payloads == [{"kind": "move_workspace", "root": str(env / "new")}]
    # the folder itself is moved by the job, so the project still points at the old root
    assert workspace.project_dir(workspace.get_project(project["id"])).parent == env / "old"


def test_the_settings_endpoint_refuses_a_collision(client, env):
    project = make_project()
    (env / "new" / project["slug"]).mkdir(parents=True)
    (env / "new" / project["slug"] / "x.txt").write_text("da", encoding="utf-8")

    body = client.get("/api/settings").json()
    body["general"]["workspaces_dir"] = str(env / "new")
    response = client.put("/api/settings", json=body)

    assert response.status_code == 409
    # nothing stored: the old path is still in effect
    assert config.get_settings().general.workspaces_dir == str(env / "old")


def test_paths_endpoint_reports_the_effective_directories(client, env):
    make_project()
    data = client.get("/api/settings/paths").json()
    assert data["workspaces_dir"] == str(env / "old")
    assert data["workspaces_configured"] is True
    assert data["workspaces_default"] == str(config.default_workspaces_dir())
    assert data["project_count"] == 1
