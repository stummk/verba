"""The configurable data directory: what follows it, and when."""

from __future__ import annotations

from verba import config, datamove, db


def _move_to(target) -> None:
    """Configure the target and let the next start carry the move out."""
    settings = config.get_settings()
    settings.general.data_dir = str(target)
    config.save_settings(settings)
    datamove.apply_pending_move()


def test_installation_files_stay_in_the_base_dir(tmp_path):
    base = config.base_data_dir()
    _move_to(tmp_path / "elsewhere")

    assert config.data_dir() == tmp_path / "elsewhere"
    assert config.logs_dir().parent == tmp_path / "elsewhere"
    # settings.json says where the data is, so it cannot live inside it;
    # site-packages is on sys.path, the rest is re-downloadable
    assert config.settings_path().parent == base
    assert config.runtime_site_packages().parent == base
    assert config.tools_dir().parent == base
    assert config.models_dir(config.get_settings()).parent == base


def test_database_and_logs_move(tmp_path):
    source = config.data_dir()
    db.init_db()
    (source / "logs").mkdir(exist_ok=True)
    (source / "logs" / "app.log").write_text("hello", encoding="utf-8")

    target = tmp_path / "backed-up"
    _move_to(target)

    assert (target / "app.db").exists()
    assert (target / "logs" / "app.log").read_text(encoding="utf-8") == "hello"
    assert not (source / "app.db").exists()
    assert db.db_path() == str(target / "app.db")


def test_move_back_to_the_default_clears_the_setting(tmp_path):
    base = config.base_data_dir()
    db.init_db()
    _move_to(tmp_path / "away")
    assert config.get_settings().general.data_dir_active == str(tmp_path / "away")

    _move_to("")
    assert config.data_dir() == base
    assert config.get_settings().general.data_dir_active == ""
    assert (base / "app.db").exists()


def test_the_old_location_stays_in_use_until_the_next_start(tmp_path):
    source = config.data_dir()
    db.init_db()
    settings = config.get_settings()
    settings.general.data_dir = str(tmp_path / "later")
    config.save_settings(settings)

    # nothing has moved yet — every read and write still goes to the old place
    assert config.data_dir() == source
    assert db.db_path() == str(source / "app.db")
    assert not (tmp_path / "later" / "app.db").exists()

    datamove.apply_pending_move()
    assert config.data_dir() == tmp_path / "later"


def test_apply_is_a_no_op_when_nothing_changed():
    assert datamove.apply_pending_move() is None


def test_an_unusable_target_does_not_stop_the_start(tmp_path):
    source = config.data_dir()
    db.init_db()
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    settings = config.get_settings()
    settings.general.data_dir = str(blocker)
    config.save_settings(settings)

    assert datamove.apply_pending_move() is None
    assert config.data_dir() == source  # still running where the data is


def test_plan_refuses_a_target_inside_the_current_dir():
    plan = datamove.move_plan(config.data_dir() / "inner")
    assert plan["problems"]


def test_plan_refuses_an_existing_database(tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "app.db").write_text("", encoding="utf-8")
    assert any("Verba-Datenbank" in problem for problem in datamove.move_plan(target)["problems"])


# ── API ───────────────────────────────────────────────────────────────


def test_api_reports_the_scheduled_move(client, tmp_path):
    target = tmp_path / "api-target"
    body = client.get("/api/settings").json()
    body["general"]["data_dir"] = str(target)
    response = client.put("/api/settings", json=body)
    assert response.status_code == 200
    assert response.json()["data_move"]["target"] == str(target)

    paths = client.get("/api/settings/paths").json()
    assert paths["data_pending"] == str(target)
    assert paths["data_dir"] == str(config.data_dir())  # unchanged until restart
    assert paths["data_default"] == str(config.base_data_dir())


def test_api_refuses_an_impossible_target(client):
    body = client.get("/api/settings").json()
    body["general"]["data_dir"] = str(config.data_dir() / "sub")
    assert client.put("/api/settings", json=body).status_code == 409


def test_api_cannot_write_the_active_location(client, tmp_path):
    body = client.get("/api/settings").json()
    body["general"]["data_dir_active"] = str(tmp_path / "sneaky")
    client.put("/api/settings", json=body)
    assert config.get_settings().general.data_dir_active == ""


def test_system_status_keeps_reminding(client, tmp_path):
    """The dashboard banner lives off this flag — it has to stay set until the
    restart has actually happened."""
    assert client.get("/api/system/status").json()["data_move_pending"] is False

    body = client.get("/api/settings").json()
    body["general"]["data_dir"] = str(tmp_path / "target")
    client.put("/api/settings", json=body)
    assert client.get("/api/system/status").json()["data_move_pending"] is True

    datamove.apply_pending_move()
    assert client.get("/api/system/status").json()["data_move_pending"] is False
