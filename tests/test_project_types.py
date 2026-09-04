from __future__ import annotations

from verba import db
from verba.services import project_types

BUILTIN_NAMES = {
    "Song",
    "Interview/Dialogue",
    "Speech",
    "Meeting Protocol",
    "Poem",
    "Roleplay",
}


def test_six_builtins_are_seeded(client):
    types = client.get("/api/types").json()
    assert {t["name"] for t in types} == BUILTIN_NAMES
    assert all(t["builtin"] == 1 for t in types)
    assert all(t["system_prompt"] for t in types)


def test_deleted_builtin_stays_deleted_after_reseed(client):
    types = client.get("/api/types").json()
    victim = next(t for t in types if t["key"] == "song")
    assert client.delete(f"/api/types/{victim['id']}").status_code == 200

    project_types.seed_builtin_types()  # what a restart would run
    names = {t["name"] for t in project_types.list_types()}
    assert "Song" not in names
    assert len(names) == 5


def test_restore_defaults_brings_builtins_back(client):
    types = client.get("/api/types").json()
    victim = next(t for t in types if t["key"] == "poem")
    client.delete(f"/api/types/{victim['id']}")

    restored = client.post("/api/types/restore-defaults").json()
    assert {t["name"] for t in restored} >= BUILTIN_NAMES


def test_custom_type_crud(client):
    created = client.post(
        "/api/types", json={"name": "Vortrag", "system_prompt": "Fasse sachlich zusammen."}
    ).json()
    assert created["builtin"] == 0
    assert created["key"] == "vortrag"

    updated = client.put(
        f"/api/types/{created['id']}",
        json={"name": "Vortrag", "system_prompt": "Neuer Prompt."},
    ).json()
    assert updated["system_prompt"] == "Neuer Prompt."

    assert client.delete(f"/api/types/{created['id']}").status_code == 200
    assert client.delete(f"/api/types/{created['id']}").status_code == 404


def test_project_gets_type_and_survives_type_deletion(client):
    types = client.get("/api/types").json()
    protokoll = next(t for t in types if t["key"] == "protocol")

    project = client.post(
        "/api/projects", json={"name": "Sitzung", "type_id": protokoll["id"]}
    ).json()
    assert project["type_id"] == protokoll["id"]

    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["type_name"] == "Meeting Protocol"
    assert "meeting minutes" in detail["type_prompt"]

    # deleting the type must not break the project (falls back to no type)
    client.delete(f"/api/types/{protokoll['id']}")
    detail = client.get(f"/api/projects/{project['id']}").json()
    assert detail["type_id"] is None
    assert detail["type_name"] is None


def test_project_type_can_be_changed(client):
    types = client.get("/api/types").json()
    lied = next(t for t in types if t["key"] == "song")
    project = client.post("/api/projects", json={"name": "Ohne Typ"}).json()
    assert project["type_id"] is None

    updated = client.put(f"/api/projects/{project['id']}", json={"type_id": lied["id"]}).json()
    assert updated["type_name"] == "Song"


def test_restore_resets_edited_builtin_prompt(client):
    types = client.get("/api/types").json()
    speech = next(t for t in types if t["key"] == "speech")
    client.put(
        f"/api/types/{speech['id']}",
        json={"name": "Speech", "system_prompt": "Custom prompt"},
    )
    client.post("/api/types/restore-defaults")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT system_prompt FROM project_types WHERE key = 'speech'"
        ).fetchone()
    assert "speech transcription" in row["system_prompt"]


def test_a_type_decides_whether_its_sections_are_kept_whole(client):
    """Page breaks per section are a layout choice of the type, off by default."""
    created = client.post(
        "/api/types", json={"name": "Liedersammlung", "system_prompt": "Lied."}
    ).json()
    assert created["keep_sections"] == 0

    updated = client.put(
        f"/api/types/{created['id']}",
        json={"name": "Liedersammlung", "system_prompt": "Lied.", "keep_sections": True},
    ).json()
    assert updated["keep_sections"] == 1

    # and it reaches the export through the project the type is assigned to
    project = client.post("/api/projects", json={"name": "Sammlung"}).json()
    project = client.put(f"/api/projects/{project['id']}", json={"type_id": created["id"]}).json()
    assert project["type_keep_sections"] == 1


def test_the_builtins_keep_the_layout_they_always_had(client):
    types = client.get("/api/types").json()
    assert all(entry["keep_sections"] == 0 for entry in types)

    song = next(entry for entry in types if entry["key"] == "song")
    client.put(
        f"/api/types/{song['id']}",
        json={"name": "Song", "system_prompt": "x", "keep_sections": True},
    )
    client.post("/api/types/restore-defaults")
    restored = client.get("/api/types").json()
    assert next(entry for entry in restored if entry["key"] == "song")["keep_sections"] == 0
