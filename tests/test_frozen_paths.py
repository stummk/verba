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


def test_repair_removes_only_the_damaged_packages(tmp_path, monkeypatch):
    """A failed installation must not cost the groups that already work."""
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "FROZEN", True)
    target = tmp_path / "data" / "site-packages"
    for name in ("numpy", "numpy.libs", "numpy-2.1.0.dist-info", "faster_whisper"):
        (target / name).mkdir(parents=True)
        (target / name / "x.py").write_text("", encoding="utf-8")
    config.mark_site_packages_repair(["numpy"])

    config.repair_site_packages()

    assert not (target / "numpy").exists()
    assert not (target / "numpy.libs").exists()
    assert not (target / "numpy-2.1.0.dist-info").exists()
    assert (target / "faster_whisper").is_dir()  # untouched
    assert not config.site_packages_repair_marker().exists()


def test_repair_is_a_noop_without_a_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "FROZEN", True)
    keep = tmp_path / "data" / "site-packages" / "numpy"
    keep.mkdir(parents=True)
    config.repair_site_packages()
    assert keep.is_dir()


def test_repair_marker_survives_a_locked_package(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config, "FROZEN", True)
    (tmp_path / "data" / "site-packages" / "numpy").mkdir(parents=True)
    config.mark_site_packages_repair(["numpy"])

    def locked_rmtree(path):
        raise PermissionError("locked")

    monkeypatch.setattr("shutil.rmtree", locked_rmtree)
    config.repair_site_packages()

    assert config.site_packages_repair_marker().exists()  # retried next start


def test_repair_marker_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.mark_site_packages_repair(["../../etc", "sub/dir"])
    assert not config.site_packages_repair_marker().exists()


def test_frozen_pip_installs_into_the_runtime_site_packages(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    command = setup_check._pip_command(["fpdf2>=2.7"])
    assert command[0] == sys.executable
    assert command[1] == setup_check.INTERNAL_PIP_FLAG
    assert "--target" in command
    assert command[command.index("--target") + 1] == str(config.runtime_site_packages())
    assert command[-1] == "fpdf2>=2.7"


def test_source_checkout_pip_uses_the_interpreter(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", False)
    command = setup_check._pip_command(["fpdf2>=2.7"])
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert "--target" not in command


def test_import_check_runs_in_a_child_process(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    assert setup_check._import_check_command("fpdf") == [
        sys.executable,
        setup_check.INTERNAL_IMPORT_FLAG,
        "fpdf",
    ]
    monkeypatch.setattr(config, "FROZEN", False)
    assert setup_check._import_check_command("fpdf") == [sys.executable, "-c", "import fpdf"]


def test_install_group_installs_and_verifies_out_of_process(monkeypatch):
    """The server process must never import a feature group itself: on Windows
    that locks its binary dependencies against the next pip run."""
    calls = {}
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(
        setup_check, "_pip_install", lambda packages, step: calls.setdefault("pip", packages)
    )
    monkeypatch.setattr(
        setup_check, "_verify_import", lambda group: calls.setdefault("verified", group.key)
    )
    group = setup_check.FEATURE_GROUPS[0]
    setup_check.install_group(group)
    assert calls == {"pip": group.packages, "verified": group.key}


def test_run_internal_task_ignores_a_normal_start():
    import run

    assert run.run_internal_task([]) is None
    assert run.run_internal_task(["--server"]) is None
    assert run.run_internal_task([run.INTERNAL_PIP_FLAG]) is None  # nothing to do


def test_run_internal_task_imports_a_module():
    import run

    assert run.run_internal_task([run.INTERNAL_IMPORT_FLAG, "json"]) == 0


def test_frozen_data_dir_is_per_user():
    path = config._frozen_data_dir()
    assert "Verba" in str(path) or "verba" in str(path)
    assert str(path).startswith(str(config.Path.home())) or "AppData" in str(path)
