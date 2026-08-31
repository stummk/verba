"""The user management as a switch: off, turning on, logging in, off again.

The central promise is that nothing changes for an installation that never
turns it on — and that turning it on loses nothing that was already there.
"""

from __future__ import annotations

import pytest

from verba import config, db
from verba.services import auth, workspace


@pytest.fixture()
def fresh(tmp_path):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    return tmp_path


def enable_admin(client, username="chef", password="geheim1234"):
    response = client.post("/api/auth/enable", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


# ── switched off: nothing changed ─────────────────────────────────────


def test_without_user_management_everything_stays_open(client, fresh):
    assert client.get("/api/auth/state").json() == {
        "enabled": False,
        "has_users": False,
        "user": None,
        "default_visibility": "private",
    }
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/apikeys").status_code == 200


def test_a_transcript_created_without_users_is_public_and_ownerless(client, fresh):
    project = client.post("/api/projects", json={"name": "Interview"}).json()
    assert project["visibility"] == "public"
    assert project["owner_id"] is None


# ── turning it on ─────────────────────────────────────────────────────


def test_enabling_adopts_the_existing_transcripts_without_touching_them(client, fresh):
    before = client.post("/api/projects", json={"name": "Altbestand"}).json()

    result = enable_admin(client)

    assert result["adopted_projects"] == 1
    assert result["user"]["role"] == "admin"
    after = client.get(f"/api/projects/{before['id']}").json()
    assert after["owner_id"] == result["user"]["id"]
    assert after["visibility"] == "public"  # unchanged — nobody is locked out
    assert after["name"] == before["name"]


def test_the_first_account_is_an_administrator_and_stays_logged_in(client, fresh):
    result = enable_admin(client)
    assert result["user"]["role"] == "admin"
    assert result["user"]["must_change_password"] == 0
    state = client.get("/api/auth/state").json()
    assert state["enabled"] is True
    assert state["user"]["username"] == "chef"


def test_enabling_twice_is_refused(client, fresh):
    enable_admin(client)
    assert (
        client.post(
            "/api/auth/enable", json={"username": "zweiter", "password": "geheim1234"}
        ).status_code
        == 409
    )


def test_a_short_password_is_refused(client, fresh):
    response = client.post("/api/auth/enable", json={"username": "chef", "password": "kurz"})
    assert response.status_code == 409
    assert "8 Zeichen" in response.json()["detail"]


# ── login ─────────────────────────────────────────────────────────────


def test_an_anonymous_request_is_rejected_once_it_is_on(client, fresh):
    enable_admin(client)
    client.post("/api/auth/logout")
    client.cookies.clear()

    assert client.get("/api/projects").status_code == 401
    # the login screen still needs to know what it is looking at
    assert client.get("/api/auth/state").status_code == 200
    assert client.get("/health").status_code == 200


def test_login_and_logout(client, fresh):
    enable_admin(client)
    client.post("/api/auth/logout")
    client.cookies.clear()

    assert (
        client.post("/api/auth/login", json={"username": "chef", "password": "falsch"}).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"username": "chef", "password": "geheim1234"}
        ).status_code
        == 200
    )
    assert client.get("/api/projects").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/projects").status_code == 401


def test_the_username_is_case_insensitive(client, fresh):
    enable_admin(client)
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert (
        client.post(
            "/api/auth/login", json={"username": "CHEF", "password": "geheim1234"}
        ).status_code
        == 200
    )


def test_a_password_is_never_returned(client, fresh):
    enable_admin(client)
    body = client.get("/api/users").text
    assert "geheim1234" not in body
    assert "password_hash" not in body


# ── self-service ──────────────────────────────────────────────────────


def test_changing_the_own_password_needs_the_old_one(client, fresh):
    enable_admin(client)
    assert (
        client.post(
            "/api/auth/password",
            json={"current_password": "falsch", "new_password": "neuespasswort"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/password",
            json={"current_password": "geheim1234", "new_password": "neuespasswort"},
        ).status_code
        == 200
    )
    # the session survives the change, the old password does not
    assert client.get("/api/projects").status_code == 200
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert (
        client.post(
            "/api/auth/login", json={"username": "chef", "password": "geheim1234"}
        ).status_code
        == 401
    )


def test_a_new_account_has_to_replace_its_start_password(client, fresh):
    enable_admin(client)
    client.post(
        "/api/users",
        json={"username": "mira", "password": "start1234", "role": "user"},
    )
    client.post("/api/auth/logout")
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": "mira", "password": "start1234"})

    blocked = client.get("/api/projects")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"
    # the interface language must still load, or the app cannot even say why
    assert client.get("/api/settings").status_code == 200

    client.post(
        "/api/auth/password",
        json={"current_password": "start1234", "new_password": "meineigenes"},
    )
    assert client.get("/api/projects").status_code == 200


# ── switching it off again ────────────────────────────────────────────


def test_disabling_reopens_the_app_and_keeps_the_accounts(client, fresh):
    enable_admin(client)
    client.post("/api/users", json={"username": "mira", "password": "start1234"})

    client.post("/api/auth/disable")
    client.cookies.clear()

    assert client.get("/api/projects").status_code == 200
    assert auth.user_count() == 2  # nobody has to be set up twice
    assert client.get("/api/auth/state").json()["enabled"] is False


def test_disabling_keeps_owners_and_visibilities_in_place(client, fresh):
    enable_admin(client)
    project = client.post("/api/projects", json={"name": "Privat", "visibility": "private"}).json()

    client.post("/api/auth/disable")

    stored = workspace.get_project(project["id"])
    assert stored["visibility"] == "private"  # only unenforced, not forgotten
    assert stored["owner_id"] == auth.get_user_by_name("chef")["id"]
    # while it is off, that private transcript is reachable by anyone again
    assert client.get(f"/api/projects/{project['id']}").status_code == 200


def test_it_can_be_switched_back_on_without_setting_everybody_up_again(client, fresh):
    enable_admin(client)
    client.post("/api/users", json={"username": "mira", "password": "start1234"})
    client.post("/api/auth/disable")
    client.cookies.clear()

    # no credentials: the accounts from before still have their passwords
    result = client.post("/api/auth/enable", json={})

    assert result.status_code == 200
    assert result.json()["reenabled"] is True
    assert result.json()["user"] is None  # nobody is signed in by flipping a switch
    assert client.get("/api/projects").status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"username": "chef", "password": "geheim1234"}
        ).status_code
        == 200
    )


def test_transcripts_created_while_it_was_off_get_an_owner_on_the_way_back_in(client, fresh):
    enable_admin(client)
    client.post("/api/auth/disable")
    orphan = client.post("/api/projects", json={"name": "Zwischendurch"}).json()
    assert orphan["owner_id"] is None

    result = client.post("/api/auth/enable", json={})

    assert result.json()["adopted_projects"] == 1
    stored = workspace.get_project(orphan["id"])
    assert stored["owner_id"] == auth.get_user_by_name("chef")["id"]
    # it was created public, so re-enabling locks nobody out of it
    assert stored["visibility"] == "public"


def test_the_first_setup_still_needs_credentials(client, fresh):
    response = client.post("/api/auth/enable", json={})
    assert response.status_code == 422
    assert auth.user_count() == 0


def test_the_settings_form_cannot_switch_the_protection_off(client, fresh):
    enable_admin(client)
    payload = client.get("/api/settings").json()
    payload["auth"]["enabled"] = False

    saved = client.put("/api/settings", json=payload)

    assert saved.status_code == 200
    assert saved.json()["auth"]["enabled"] is True
    assert config.get_settings().auth.enabled is True


def test_the_default_visibility_is_configurable(client, fresh):
    enable_admin(client)
    payload = client.get("/api/settings").json()
    payload["auth"]["default_visibility"] = "shared"
    client.put("/api/settings", json=payload)

    project = client.post("/api/projects", json={"name": "Neu"}).json()

    assert project["visibility"] == "shared"


# ── service level ─────────────────────────────────────────────────────


def test_the_password_hash_is_salted_per_user(fresh):
    first, salt_a = auth.hash_password("gleichespasswort")
    second, salt_b = auth.hash_password("gleichespasswort")
    assert salt_a != salt_b
    assert first != second
    assert auth.verify_password("gleichespasswort", first, salt_a)
    assert not auth.verify_password("anderes", first, salt_a)


def test_an_expired_session_does_not_authenticate(fresh):
    admin = auth.create_user("chef", "geheim1234", role=auth.ROLE_ADMIN)
    token = auth.create_session(admin["id"])
    assert auth.session_user(token) is not None

    with db.get_conn() as conn:
        conn.execute("UPDATE sessions SET expires_at = '2000-01-01 00:00:00'")

    assert auth.session_user(token) is None


def test_only_the_hash_of_a_session_token_is_stored(fresh):
    admin = auth.create_user("chef", "geheim1234", role=auth.ROLE_ADMIN)
    token = auth.create_session(admin["id"])
    with db.get_conn() as conn:
        [row] = conn.execute("SELECT token_hash FROM sessions").fetchall()
    assert token not in row["token_hash"]


def test_visibility_falls_closed_without_a_user(fresh):
    """A caller that forgot its own check must get nothing, not everything."""
    auth.create_user("chef", "geheim1234", role=auth.ROLE_ADMIN)
    settings = config.get_settings()
    settings.auth.enabled = True
    config.save_settings(settings)
    workspace.create_project("Egal")

    assert workspace.list_projects(None) == []
