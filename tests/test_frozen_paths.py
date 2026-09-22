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


def test_the_setup_can_point_the_data_directory_elsewhere(tmp_path, monkeypatch):
    """The Windows installer asks for the directory and leaves it next to the exe.

    That answer is the way out of a profile whose AppData the installation
    cannot use — no environment variable, no registry key, just a file that
    belongs to this installation and is there at every logon.
    """
    program = tmp_path / "program"
    program.mkdir()
    target = tmp_path / "anderswo"
    (program / config.DATA_DIR_MARKER).write_text(f"{target}\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(program / "Verba.exe"))
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.delenv("VERBA_DATA_DIR", raising=False)
    monkeypatch.delenv("TRANSKRIPTOR_DATA_DIR", raising=False)

    assert config.chosen_data_dir() == target
    assert config.base_data_dir() == target


def test_an_unanswered_marker_leaves_the_per_user_directory_alone(tmp_path, monkeypatch):
    """An empty file is not a choice — and a missing one never was."""
    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.setattr(sys, "executable", str(program / "Verba.exe"))
    assert config.chosen_data_dir() is None

    (program / config.DATA_DIR_MARKER).write_text("   \n", encoding="utf-8")
    assert config.chosen_data_dir() is None


def test_the_environment_still_wins_over_the_installers_answer(tmp_path, monkeypatch):
    """Tests and a server start pass VERBA_DATA_DIR, and that has to keep winning."""
    program = tmp_path / "program"
    program.mkdir()
    (program / config.DATA_DIR_MARKER).write_text(str(tmp_path / "vom-setup"), encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(program / "Verba.exe"))
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "aus-der-umgebung"))

    assert config.base_data_dir() == tmp_path / "aus-der-umgebung"


def test_the_installer_and_the_app_agree_on_the_marker():
    """Nothing but this test ties the setup script to `config.py`.

    The installer writes the chosen directory next to the program and the app
    reads it from there; a renamed file would go unnoticed until an
    installation quietly used the wrong directory. The two `runasoriginaluser`
    flags are here for the same reason — Setup is elevated, and a Verba it
    starts must not be, or the packages it installs land in the user's data
    directory as somebody else.
    """
    script = (config.PROJECT_ROOT / "packaging" / "verba.iss").read_text(encoding="utf-8-sig")
    assert f"{{app}}\\{config.DATA_DIR_MARKER}" in script
    assert "Permissions: users-modify" in script
    starts = [line for line in script.splitlines() if line.startswith("Filename:")]
    assert starts and all("runasoriginaluser" in line for line in starts)


def test_frozen_data_dir_is_per_user():
    path = config._frozen_data_dir()
    assert "Verba" in str(path) or "verba" in str(path)
    assert str(path).startswith(str(config.Path.home())) or "AppData" in str(path)
