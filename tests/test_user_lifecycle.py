"""Deleting an account, and the rules that keep an installation reachable.

The strategy: a private transcript belonged to that person alone and goes
with them; a shared or public one is other people's working material and
changes hands to the longest-serving administrator instead of disappearing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verba import config, db
from verba.services import auth, workspace


@pytest.fixture()
def env(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    settings.auth.enabled = True
    config.save_settings(settings)
    db.init_db()
    return tmp_path


@pytest.fixture()
def people(env):
    return {
        "admin": auth.create_user(
            "chef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False
        ),
        "mira": auth.create_user("mira", "geheim1234", must_change_password=False),
        "jonas": auth.create_user("jonas", "geheim1234", must_change_password=False),
    }


def owned(user, name, visibility):
    return workspace.create_project(name, owner_id=user["id"], visibility=visibility)


# ── the deletion strategy ─────────────────────────────────────────────


def test_private_transcripts_go_with_the_account(people):
    project = owned(people["mira"], "Privat", "private")
    folder = Path(project["workspace"])
    assert folder.is_dir()

    result = auth.delete_user(people["mira"]["id"])

    assert result == {"deleted_projects": 1, "transferred_projects": 0}
    assert workspace.get_project(project["id"]) is None
    assert not folder.exists()  # the audio goes too, not just the row


def test_shared_and_public_ones_change_hands_instead(people):
    shared = owned(people["mira"], "Geteilt", "shared")
    public = owned(people["mira"], "Offen", "public")
    auth.set_shares(shared["id"], [people["jonas"]["id"]])

    result = auth.delete_user(people["mira"]["id"])

    assert result == {"deleted_projects": 0, "transferred_projects": 2}
    for project_id in (shared["id"], public["id"]):
        project = workspace.get_project(project_id)
        assert project is not None
        assert project["owner_id"] == people["admin"]["id"]
    # the people it was shared with keep it
    assert auth.list_shares(shared["id"]) == [people["jonas"]["id"]]


def test_the_successor_is_the_longest_serving_administrator(people):
    later_admin = auth.create_user(
        "zweitchef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False
    )
    project = owned(people["mira"], "Offen", "public")

    auth.delete_user(people["mira"]["id"])

    assert workspace.get_project(project["id"])["owner_id"] == people["admin"]["id"]
    assert later_admin["id"] != people["admin"]["id"]


def test_a_deleted_user_loses_the_shares_that_were_granted_to_them(people):
    project = owned(people["mira"], "Geteilt", "shared")
    auth.set_shares(project["id"], [people["jonas"]["id"]])

    auth.delete_user(people["jonas"]["id"])

    assert auth.list_shares(project["id"]) == []


def test_deleting_an_administrator_hands_their_work_to_the_next_one(people):
    second = auth.create_user(
        "zweitchef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False
    )
    project = owned(second, "Offen", "public")

    auth.delete_user(second["id"])

    assert workspace.get_project(project["id"])["owner_id"] == people["admin"]["id"]


# ── the installation stays reachable ──────────────────────────────────


def test_the_last_administrator_cannot_be_deleted(env):
    admin = auth.create_user("chef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False)
    auth.create_user("mira", "geheim1234")

    with pytest.raises(auth.AuthError, match="letzte Administratorkonto"):
        auth.delete_user(admin["id"])


def test_the_last_administrator_cannot_be_demoted(env):
    admin = auth.create_user("chef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False)

    with pytest.raises(auth.AuthError, match="letzte Administratorkonto"):
        auth.update_user(admin["id"], role=auth.ROLE_USER)


def test_with_a_second_administrator_both_are_allowed(people):
    second = auth.create_user(
        "zweitchef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False
    )
    assert auth.update_user(second["id"], role=auth.ROLE_USER)["role"] == "user"
    auth.update_user(second["id"], role=auth.ROLE_ADMIN)
    auth.delete_user(second["id"])
    assert auth.admin_count() == 1


# ── through the API ───────────────────────────────────────────────────


def login(client, username, password="geheim1234"):
    assert (
        client.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).status_code
        == 200
    )


def test_a_user_can_delete_their_own_account(client, people):
    login(client, "mira")
    private = owned(people["mira"], "Privat", "private")
    public = owned(people["mira"], "Offen", "public")

    response = client.request("DELETE", "/api/auth/me", json={"password": "geheim1234"})

    assert response.json() == {"deleted_projects": 1, "transferred_projects": 1}
    assert workspace.get_project(private["id"]) is None
    assert workspace.get_project(public["id"])["owner_id"] == people["admin"]["id"]
    assert client.get("/api/projects").status_code == 401  # session gone with the account


def test_deleting_the_own_account_needs_the_password(client, people):
    login(client, "mira")
    response = client.request("DELETE", "/api/auth/me", json={"password": "falsch"})
    assert response.status_code == 403
    assert auth.get_user(people["mira"]["id"]) is not None


def test_the_last_administrator_is_refused_at_the_api_too(client, env):
    auth.create_user("chef", "geheim1234", role=auth.ROLE_ADMIN, must_change_password=False)
    login(client, "chef")
    response = client.request("DELETE", "/api/auth/me", json={"password": "geheim1234"})
    assert response.status_code == 409


def test_an_administrator_resetting_a_password_sets_a_start_password(client, people):
    login(client, "chef")

    updated = client.put(
        f"/api/users/{people['mira']['id']}", json={"password": "zurueckgesetzt"}
    ).json()

    assert updated["must_change_password"] == 1


def test_a_user_who_lost_their_password_cannot_reuse_the_old_one(client, people):
    login(client, "chef")
    client.put(f"/api/users/{people['mira']['id']}", json={"password": "zurueckgesetzt"})
    client.post("/api/auth/logout")
    client.cookies.clear()

    assert (
        client.post(
            "/api/auth/login", json={"username": "mira", "password": "geheim1234"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": "mira", "password": "zurueckgesetzt"}
        ).status_code
        == 200
    )


def test_a_duplicate_username_is_refused(client, people):
    login(client, "chef")
    response = client.post("/api/users", json={"username": "MIRA", "password": "geheim1234"})
    assert response.status_code == 409
    assert "bereits vergeben" in response.json()["detail"]
