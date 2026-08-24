"""Path resolution and pip dispatch for PyInstaller (frozen) builds."""

from __future__ import annotations

import sys

from verba import config, setup_check


def test_data_dir_uses_user_dir_when_frozen(tmp_path, monkeypatch):
    monkeypatch.delenv("VERBA_DATA_DIR", raising=False)
    monkeypatch.delenv("TRANSKRIPTOR_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(config, "_frozen_data_dir", lambda: tmp_path / "userdata")
    assert config.data_dir() == tmp_path / "userdata"
    assert (tmp_path / "userdata").is_dir()


def test_data_dir_env_var_wins_even_when_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "envdata"))
    monkeypatch.setattr(config, "FROZEN", True)
    assert config.data_dir() == tmp_path / "envdata"


def test_bundle_root_is_project_root_in_source_checkout():
    assert (config.bundle_root() / "frontend" / "index.html").exists()
    assert (config.bundle_root() / "docs" / "user" / "de.md").exists()


def test_ensure_runtime_site_packages(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "FROZEN", True)
    config.ensure_runtime_site_packages()
    target = str(tmp_path / "data" / "site-packages")
    assert target in sys.path
    sys.path.remove(target)

    monkeypatch.setattr(config, "FROZEN", False)
    config.ensure_runtime_site_packages()  # no-op in source checkouts
    assert target not in sys.path


def test_repair_marker_wipes_site_packages_on_start(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "FROZEN", True)
    debris = tmp_path / "data" / "site-packages" / "broken_pkg"
    debris.mkdir(parents=True)
    (debris / "x.py").write_text("", encoding="utf-8")
    config.site_packages_repair_marker().touch()

    config.ensure_runtime_site_packages()
    target = tmp_path / "data" / "site-packages"
    assert target.is_dir() and not (target / "broken_pkg").exists()
    assert not config.site_packages_repair_marker().exists()
    sys.path.remove(str(target))


def test_repair_marker_survives_locked_site_packages(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "FROZEN", True)
    target = tmp_path / "data" / "site-packages"
    target.mkdir(parents=True)
    config.site_packages_repair_marker().touch()

    def locked_rmtree(path):
        raise PermissionError("locked")

    monkeypatch.setattr("shutil.rmtree", locked_rmtree)
    config.ensure_runtime_site_packages()

    assert config.site_packages_repair_marker().exists()
    assert target.is_dir()
    sys.path.remove(str(target))


def test_install_group_dispatches_to_frozen_pip(monkeypatch):
    calls = {}
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(
        setup_check, "_pip_install_frozen", lambda packages: calls.setdefault("frozen", packages)
    )
    monkeypatch.setattr(setup_check, "group_installed", lambda group: True)
    group = setup_check.FEATURE_GROUPS[0]
    setup_check.install_group(group)
    assert calls["frozen"] == group.packages


def test_frozen_data_dir_is_per_user():
    path = config._frozen_data_dir()
    assert "Verba" in str(path) or "verba" in str(path)
    assert str(path).startswith(str(config.Path.home())) or "AppData" in str(path)
