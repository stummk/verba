"""Who may reach which transcript, and what a normal user may do at all.

Visibility decides access, and access is complete: whoever can see a
transcript can also edit and delete it. The two things that stay with the
owner are the visibility itself and the share list.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from verba import config, db
from verba.main import create_app
from verba.services import auth, workspace


@pytest.fixture()
def env(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    return tmp_path


@pytest.fixture()
def team(env):
    """An enabled installation with an admin and two normal users."""
    admin = auth.create_user("chef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False)
    mira = auth.create_user("mira", "geheim1234", must_change_password=False)
    jonas = auth.create_user("jonas", "geheim1234", must_change_password=False)
    settings = config.get_settings()
    settings.auth.enabled = True
    config.save_settings(settings)
    return {"admin": admin, "mira": mira, "jonas": jonas}


def login(username: str) -> TestClient:
    client = TestClient(create_app())
    client.__enter__()
    response = client.post("/api/auth/login", json={"username": username, "password": "geheim1234"})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture()
def as_mira(team):
    client = login("mira")
    yield client
    client.__exit__(None, None, None)


@pytest.fixture()
def as_jonas(team):
    client = login("jonas")
    yield client
    client.__exit__(None, None, None)


@pytest.fixture()
def as_admin(team):
    client = login("chef")
    yield client
    client.__exit__(None, None, None)


def make_project(owner, name="Interview", visibility="private", shared_with=()):
    project = workspace.create_project(name, owner_id=owner["id"], visibility=visibility)
    if shared_with:
        auth.set_shares(project["id"], [u["id"] for u in shared_with])
    return project


# ── private ───────────────────────────────────────────────────────────


def test_a_private_transcript_belongs_to_its_owner_alone(team, as_mira, as_jonas):
    project = make_project(team["mira"], visibility="private")

    assert [p["id"] for p in as_mira.get("/api/projects").json()] == [project["id"]]
    assert as_jonas.get("/api/projects").json() == []
    # not 403: whether it exists at all is already too much to tell
    assert as_jonas.get(f"/api/projects/{project['id']}").status_code == 404
    assert as_jonas.delete(f"/api/projects/{project['id']}").status_code == 404


def test_an_administrator_reaches_every_transcript(team, as_admin):
    project = make_project(team["mira"], visibility="private")
    assert as_admin.get(f"/api/projects/{project['id']}").status_code == 200
    assert [p["id"] for p in as_admin.get("/api/projects").json()] == [project["id"]]


# ── shared ────────────────────────────────────────────────────────────


def test_a_shared_transcript_reaches_exactly_the_named_users(team, as_mira, as_jonas):
    shared = make_project(team["mira"], visibility="shared", shared_with=[team["jonas"]])
    make_project(team["mira"], name="Nur meins", visibility="private")

    assert [p["id"] for p in as_jonas.get("/api/projects").json()] == [shared["id"]]
    assert as_jonas.get(f"/api/projects/{shared['id']}").status_code == 200


def test_dropping_someone_from_the_share_list_takes_the_access_with_it(team, as_mira, as_jonas):
    project = make_project(team["mira"], visibility="shared", shared_with=[team["jonas"]])
    assert as_jonas.get(f"/api/projects/{project['id']}").status_code == 200

    as_mira.put(
        f"/api/projects/{project['id']}/visibility",
        json={"visibility": "shared", "user_ids": []},
    )

    assert as_jonas.get(f"/api/projects/{project['id']}").status_code == 404


# ── public: seeing it means full access ───────────────────────────────


def test_a_public_transcript_may_be_edited_by_anyone(team, as_mira, as_jonas):
    project = make_project(team["mira"], visibility="public")

    renamed = as_jonas.put(f"/api/projects/{project['id']}", json={"name": "Umbenannt"})

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Umbenannt"


def test_but_its_visibility_stays_with_the_owner(team, as_mira, as_jonas):
    """Full access must not include the power to lock everyone else out."""
    project = make_project(team["mira"], visibility="public")

    refused = as_jonas.put(
        f"/api/projects/{project['id']}/visibility", json={"visibility": "private"}
    )

    assert refused.status_code == 403
    assert (
        as_mira.put(
            f"/api/projects/{project['id']}/visibility", json={"visibility": "private"}
        ).status_code
        == 200
    )


def test_the_owner_and_the_administrator_may_change_it(team, as_admin):
    project = make_project(team["mira"], visibility="private")
    changed = as_admin.put(
        f"/api/projects/{project['id']}/visibility",
        json={"visibility": "shared", "user_ids": [team["jonas"]["id"]]},
    )
    assert changed.status_code == 200
    assert changed.json()["shared_with"] == [team["jonas"]["id"]]


def test_switching_away_from_shared_clears_the_share_list(team, as_mira):
    project = make_project(team["mira"], visibility="shared", shared_with=[team["jonas"]])
    as_mira.put(f"/api/projects/{project['id']}/visibility", json={"visibility": "private"})
    assert auth.list_shares(project["id"]) == []


# ── files, segments, exports and jobs follow the transcript ───────────


def test_the_file_routes_are_closed_for_a_foreign_private_transcript(team, as_jonas, env):
    project = make_project(team["mira"], visibility="private")
    source = env / "lied.mp3"
    source.write_bytes(b"x")
    [file_row] = workspace.import_paths(workspace.get_project(project["id"]), [str(source)])

    for method, path in [
        ("get", f"/api/files/{file_row['id']}/segments"),
        ("get", f"/api/files/{file_row['id']}/texts"),
        ("get", f"/api/files/{file_row['id']}/audio"),
        ("delete", f"/api/files/{file_row['id']}"),
        ("get", f"/api/projects/{project['id']}/exports"),
    ]:
        response = getattr(as_jonas, method)(path)
        assert response.status_code == 404, f"{method} {path} → {response.status_code}"


def test_the_job_list_only_names_reachable_transcripts(team, as_mira, as_jonas):
    from verba.core.jobs import job_queue

    project = make_project(team["mira"], visibility="private")
    job = job_queue.enqueue("export_pdf", payload={"scope": "project"}, project_id=project["id"])

    assert [j["id"] for j in as_mira.get("/api/jobs").json()] == [job["id"]]
    assert as_jonas.get("/api/jobs").json() == []
    assert as_jonas.post(f"/api/jobs/{job['id']}/cancel").status_code == 404


def test_the_search_never_returns_a_foreign_private_transcript(team):
    """The index is global, so this is the filter that actually matters."""
    private = make_project(team["mira"], visibility="private")
    public = make_project(team["jonas"], name="Offen", visibility="public")

    clause, params = auth.visibility_clause(team["jonas"])
    with db.get_conn() as conn:
        rows = conn.execute(f"SELECT p.id FROM projects p WHERE {clause}", params).fetchall()

    ids = {row["id"] for row in rows}
    assert public["id"] in ids
    assert private["id"] not in ids


# ── what a normal user may not do ─────────────────────────────────────


ADMIN_ONLY = [
    ("get", "/api/users", None),
    ("post", "/api/users", {"username": "neu", "password": "geheim1234"}),
    ("get", "/api/settings/paths", None),
    ("get", "/api/system/info", None),
    ("post", "/api/system/setup/run", {"include_optional": False}),
    ("post", "/api/system/shutdown", None),
    ("get", "/api/apikeys", None),
    ("post", "/api/apikeys", {"name": "k"}),
    ("post", "/api/types", {"name": "Neu"}),
    ("post", "/api/types/restore-defaults", None),
    ("post", "/api/models/download", {"name": "small"}),
    ("post", "/api/models/llm/stop", None),
    ("get", "/api/search/models", None),
    ("post", "/api/search/reindex", None),
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ONLY)
def test_a_normal_user_is_refused_on_administrative_routes(as_mira, method, path, body):
    response = getattr(as_mira, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 403, f"{method} {path} → {response.status_code}"


def test_a_normal_user_only_sees_their_own_settings(as_mira):
    settings = as_mira.get("/api/settings").json()
    assert settings["restricted"] is True
    assert set(settings["general"]) == {"ui_language"}
    assert "api_key" not in settings.get("llm", {})
    assert "workspaces_dir" not in settings.get("general", {})


def test_a_normal_user_may_still_read_the_types_and_the_guide(as_mira):
    assert as_mira.get("/api/types").status_code == 200
    assert as_mira.get("/api/docs?lang=de").status_code == 200
    assert as_mira.get("/api/models").status_code == 200


def test_an_administrator_passes_all_of_them(as_admin):
    assert as_admin.get("/api/users").status_code == 200
    assert as_admin.get("/api/apikeys").status_code == 200
    assert as_admin.get("/api/settings/paths").status_code == 200
