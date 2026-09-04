"""Verba updates itself from its own GitHub releases.

Three installations, three artifacts, and one rule that matters more than the
rest: nothing is installed that has not been recognised as the right artifact
for this installation. The tests therefore cover the version comparison, the
asset that belongs to each kind, the refusals, and the two installations that
can be exercised without a Windows installer — an AppImage file and a server
package — including that the version they replace is gone afterwards and how
the app asks to be restarted.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from verba import __version__, config
from verba.services import updates

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fresh_state():
    """Every test starts without a cached check and without a restart request."""
    updates._check.update(checked_at=0.0, version="", url="", notes="", asset=None, error="")
    updates._install.update(
        running=False, percent=0, detail="", error="", log=[], version="", finished_at=0.0
    )
    updates._restart.update(mode=updates.RESTART_NONE, command=[])
    yield


# ── version comparison ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.1.2", "0.1.1", True),
        ("0.2.0", "0.1.9", True),
        ("1.0", "0.9.9", True),
        ("0.1.1", "0.1.1", False),
        ("0.1.0", "0.1.1", False),
        ("0.1.1", "0.1.1-rc1", True),  # the finished release beats its pre-release
        ("0.1.1-rc2", "0.1.1", False),
        ("", "0.1.1", False),
        ("release", "0.1.1", False),
    ],
)
def test_only_a_later_version_counts_as_an_update(latest, current, expected):
    assert updates.is_newer(latest, current) is expected


def test_a_tag_may_carry_a_v_prefix():
    assert updates.parse_version("v1.2.3") == ((1, 2, 3), "")
    assert updates.is_newer("v9.0.0", "1.0.0") is True


# ── the asset that belongs to an installation ─────────────────────────

RELEASE_ASSETS = [
    {"name": "Verba-Setup-0.2.0.exe", "browser_download_url": "https://example/setup", "size": 10},
    {
        "name": "Verba-0.2.0-x86_64.AppImage",
        "browser_download_url": "https://example/appimage",
        "size": 10,
    },
    {"name": "verba-server-0.2.0.zip", "browser_download_url": "https://example/zip", "size": 10},
]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (updates.WINDOWS_INSTALLER, "Verba-Setup-0.2.0.exe"),
        (updates.APPIMAGE, "Verba-0.2.0-x86_64.AppImage"),
        (updates.SERVER_ZIP, "verba-server-0.2.0.zip"),
        (updates.SOURCE, None),
    ],
)
def test_every_kind_picks_its_own_artifact(kind, expected):
    asset = updates._asset_for(kind, RELEASE_ASSETS)
    assert (asset or {}).get("name") == expected


def test_the_release_pipeline_builds_exactly_these_names():
    """The names matched here are the ones the workflow uploads."""
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for kind, name in (
        (updates.WINDOWS_INSTALLER, "Verba-Setup-"),
        (updates.APPIMAGE, "Verba-"),
        (updates.SERVER_ZIP, "verba-server-"),
    ):
        assert name in workflow
        assert updates._asset_for(kind, RELEASE_ASSETS) is not None


def test_an_asset_name_that_is_not_a_file_name_is_ignored():
    """The name becomes a path on disk, so it is validated, not trusted."""
    hostile = [
        {"name": "../../evil.exe", "browser_download_url": "https://example/x", "size": 1},
        {"name": "Verba-Setup-0.2.0.exe", "browser_download_url": "", "size": 1},
    ]
    assert updates._asset_for(updates.WINDOWS_INSTALLER, hostile) is None


# ── what this installation is ─────────────────────────────────────────


def test_a_source_checkout_is_left_to_git(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", False)
    assert updates.installation_kind() == updates.SOURCE
    ok, reason = updates.installable()
    assert ok is False
    assert "git" in reason


def test_an_unpacked_server_package_updates_itself(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FROZEN", False)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)  # no .git next to it
    assert updates.installation_kind() == updates.SERVER_ZIP
    assert updates.installable() == (True, "")


def test_a_frozen_windows_build_updates_through_its_installer(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(updates.platform, "system", lambda: "Windows")
    assert updates.installation_kind() == updates.WINDOWS_INSTALLER
    assert updates.installable() == (True, "")


def test_a_frozen_build_without_an_appimage_has_no_update_path(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(updates.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert updates.installation_kind() == updates.UNSUPPORTED
    ok, reason = updates.installable()
    assert ok is False
    assert "Darwin" in reason


def test_an_appimage_that_cannot_be_written_says_so(monkeypatch, tmp_path):
    image = tmp_path / "Verba-0.1.1-x86_64.AppImage"
    image.write_bytes(b"\x7fELF")
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(updates.platform, "system", lambda: "Linux")
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert updates.installation_kind() == updates.APPIMAGE
    assert updates.installable() == (True, "")

    monkeypatch.setattr(updates.os, "access", lambda path, mode: False)
    ok, reason = updates.installable()
    assert ok is False
    assert str(tmp_path) in reason


# ── the check ─────────────────────────────────────────────────────────


def release(version: str = "0.2.0") -> dict:
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://example/releases/{version}",
        "body": "Was neu ist",
        "assets": RELEASE_ASSETS,
    }


def test_the_check_reports_a_newer_release(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", False)
    monkeypatch.setattr(config, "PROJECT_ROOT", Path("/nowhere"))  # server-zip kind
    monkeypatch.setattr(updates.os, "access", lambda path, mode: True)
    monkeypatch.setattr(updates, "_get_json", lambda url: release("9.9.9"))

    data = updates.check()

    assert data["current"] == __version__
    assert data["latest"] == "9.9.9"
    assert data["available"] is True
    assert data["can_install"] is True
    assert data["notes"] == "Was neu ist"


def test_the_check_is_cached_and_refreshed_on_request(monkeypatch):
    calls = []

    def fake_get(url):
        calls.append(url)
        return release("9.9.9")

    monkeypatch.setattr(updates, "_get_json", fake_get)
    updates.check()
    updates.check()
    assert len(calls) == 1
    updates.check(force=True)
    assert len(calls) == 2


def test_a_failed_check_is_not_an_error_the_user_has_to_handle(monkeypatch):
    def refuse(url):
        raise OSError("no route to host")

    monkeypatch.setattr(updates, "_get_json", refuse)
    data = updates.check()
    assert data["available"] is False
    assert "no route to host" in data["error"]


def test_a_release_without_an_artifact_for_this_kind_cannot_be_installed(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(updates.platform, "system", lambda: "Windows")
    empty = release("9.9.9") | {"assets": [{"name": "notes.txt", "size": 1}]}
    monkeypatch.setattr(updates, "_get_json", lambda url: empty)

    data = updates.check()

    assert data["available"] is True
    assert data["can_install"] is False
    assert "kein Paket" in data["reason"]


def test_the_background_check_stays_away_from_a_source_checkout(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", False)  # the repository has a .git
    assert updates.start_background_checks() is False


def test_the_background_check_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(updates, "installation_kind", lambda: updates.SERVER_ZIP)
    settings = config.get_settings()
    settings.updates.check_enabled = False
    config.save_settings(settings)
    assert updates.start_background_checks() is False


# ── installing an AppImage ────────────────────────────────────────────


def prepared_state(monkeypatch, version: str = "9.9.9") -> None:
    """A check result that offers `version`, without touching the network."""
    monkeypatch.setattr(updates, "_get_json", lambda url: release(version))


def test_an_appimage_update_replaces_the_running_file_and_relaunches(monkeypatch, tmp_path):
    image = tmp_path / "Verba-0.1.1-x86_64.AppImage"
    image.write_bytes(b"\x7fELF-old")
    monkeypatch.setenv("APPIMAGE", str(image))
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(updates.platform, "system", lambda: "Linux")
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")

    downloaded = tmp_path / "download" / "Verba-9.9.9-x86_64.AppImage"
    downloaded.parent.mkdir()
    downloaded.write_bytes(b"\x7fELF-new")
    stopped: list[float] = []
    monkeypatch.setattr(updates.lifecycle, "stop_process", lambda delay=0.1: stopped.append(delay))

    updates._install_appimage(downloaded, "9.9.9")

    assert image.read_bytes() == b"\x7fELF-new"
    # the old version is gone, and so is the file it was staged as
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "Verba-0.1.1-x86_64.AppImage",
        "download",
    ]
    assert updates.pending_restart() == (updates.RESTART_EXEC, [str(image)])
    assert stopped, "the app has to go down for the new version to come up"


def test_a_download_that_is_not_an_appimage_is_never_installed(monkeypatch, tmp_path):
    image = tmp_path / "Verba.AppImage"
    image.write_bytes(b"\x7fELF-old")
    monkeypatch.setenv("APPIMAGE", str(image))
    error_page = tmp_path / "error.AppImage"
    error_page.write_bytes(b"<html>403</html>")

    with pytest.raises(RuntimeError, match="AppImage"):
        updates._install_appimage(error_page, "9.9.9")

    assert image.read_bytes() == b"\x7fELF-old"
    assert updates.pending_restart() == (updates.RESTART_NONE, [])


# ── installing a server package ───────────────────────────────────────


def server_package(tmp_path: Path, version: str = "9.9.9") -> Path:
    """A zip shaped like the one the release pipeline assembles."""
    root = tmp_path / "package" / f"verba-server-{version}"
    for item in updates.PACKAGE_ITEMS:
        target = root / item
        if item.endswith((".py", ".sh", ".md")):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {item} {version}\n", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "marker.txt").write_text(version, encoding="utf-8")
    (root / "requirements" / "core.txt").write_text("fastapi\n", encoding="utf-8")

    archive = tmp_path / f"verba-server-{version}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(root.parent).as_posix())
    return archive


def test_a_server_update_replaces_the_application_and_removes_the_old_one(monkeypatch, tmp_path):
    installation = tmp_path / "opt" / "verba"
    for item in updates.PACKAGE_ITEMS:
        target = installation / item
        if item.endswith((".py", ".sh", ".md")):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("old\n", encoding="utf-8")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "marker.txt").write_text("old", encoding="utf-8")
    # runtime data, settings and the virtualenv are not part of a release
    (installation / "data").mkdir()
    (installation / "data" / "app.db").write_bytes(b"sqlite")
    (installation / "data" / "settings.json").write_text("{}", encoding="utf-8")
    (installation / ".venv").mkdir()
    (installation / "workspaces").mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", installation)

    pip_calls: list[list[str]] = []
    monkeypatch.setattr(updates, "_pip_install", lambda path: pip_calls.append([str(path)]))
    monkeypatch.setenv("INVOCATION_ID", "systemd")  # started as a service
    monkeypatch.setattr(updates.lifecycle, "stop_process", lambda delay=0.1: None)

    archive = server_package(tmp_path)
    updates._install_server_package(archive, "9.9.9")

    assert (installation / "run.py").read_text(encoding="utf-8") == "# run.py 9.9.9\n"
    assert (installation / "backend" / "marker.txt").read_text(encoding="utf-8") == "9.9.9"
    assert (installation / "data" / "app.db").read_bytes() == b"sqlite"
    assert (installation / "data" / "settings.json").exists()
    assert (installation / ".venv").is_dir() and (installation / "workspaces").is_dir()
    # nothing of the old version is left — neither in place nor as a backup
    assert not list(installation.glob("*.replaced"))
    assert not list(updates.download_dir().glob("previous*"))
    assert not (updates.download_dir() / "unpacked").exists()
    assert pip_calls, "the new dependencies are installed before the files are replaced"
    assert updates.pending_restart() == (updates.RESTART_EXIT, [])


def test_a_server_update_without_a_service_manager_only_asks_for_a_restart(monkeypatch, tmp_path):
    installation = tmp_path / "srv"
    installation.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", installation)
    monkeypatch.setattr(updates, "_pip_install", lambda path: None)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    updates._install_server_package(server_package(tmp_path), "9.9.9")

    assert updates.pending_restart() == (updates.RESTART_NONE, [])
    assert "bitte Verba neu starten" in updates.state()["log"][-1]


def test_an_incomplete_server_package_changes_nothing(monkeypatch, tmp_path):
    installation = tmp_path / "srv"
    (installation / "backend").mkdir(parents=True)
    (installation / "backend" / "marker.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", installation)

    incomplete = tmp_path / "verba-server-9.9.9.zip"
    with zipfile.ZipFile(incomplete, "w") as bundle:
        bundle.writestr("verba-server-9.9.9/README.md", "only this")

    with pytest.raises(RuntimeError, match="fehlt"):
        updates._install_server_package(incomplete, "9.9.9")

    assert (installation / "backend" / "marker.txt").read_text(encoding="utf-8") == "old"


def test_a_server_package_never_writes_outside_its_directory(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../../etc/cron.d/evil", "boom")
    with zipfile.ZipFile(archive) as bundle, pytest.raises(RuntimeError, match="Unerwarteter Pfad"):
        updates._reject_unsafe_members(bundle)


# ── the log while it runs, and nothing afterwards ──────────────────────


def test_the_log_grows_while_the_installation_runs(monkeypatch):
    """Every step is broadcast with the whole log, which is what the UI shows."""
    published: list[dict] = []
    monkeypatch.setattr(
        updates.hub,
        "publish",
        lambda event, payload: published.append({"event": event, **payload}),
    )

    updates._emit(10, "Lade Verba-9.9.9-x86_64.AppImage ...")
    updates._emit(11, "")  # a percent tick adds no line
    updates._emit(100, "Version 9.9.9 installiert", state="done")

    assert [entry["event"] for entry in published] == ["update.progress"] * 3
    assert published[-1]["log"] == [
        "Lade Verba-9.9.9-x86_64.AppImage ...",
        "Version 9.9.9 installiert",
    ]
    assert published[-1]["state"] == "done"
    assert updates.state()["log"] == published[-1]["log"]


def test_nothing_of_an_update_is_left_on_disk(tmp_path):
    """The log is not persisted, and the artifact is gone at the next start."""
    updates._emit(50, "Lade verba-server-9.9.9.zip ...")
    (updates.download_dir() / "verba-server-9.9.9.zip").write_bytes(b"PK")
    updates._installer_log().write_text("Setup finished\n", encoding="utf-8")

    updates.cleanup_downloads()

    assert not updates.download_dir().exists() or not list(updates.download_dir().iterdir())
    assert not list(config.tools_dir().glob("*.json"))


def test_a_refusing_installer_is_quoted_from_its_own_log():
    """The one moment the installer's log is read: it says why it gave up."""
    updates._installer_log().write_text("Log opened\nSetup aborted\nGot EACCES\n", encoding="utf-8")
    message = updates._installer_error(3)
    assert "Code 3" in message
    assert "Got EACCES" in message


# ── refusals of the start ─────────────────────────────────────────────


def test_no_update_is_started_when_the_version_is_the_newest(monkeypatch):
    monkeypatch.setattr(updates, "_get_json", lambda url: release(__version__))
    result = updates.start_update()
    assert result == {"started": False, "reason": "Verba ist auf dem neuesten Stand."}


def test_no_update_is_started_for_a_source_checkout(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", False)
    monkeypatch.setattr(updates, "_get_json", lambda url: release("9.9.9"))
    result = updates.start_update()
    assert result["started"] is False
    assert "git" in result["reason"]


def test_a_running_update_is_not_started_twice(monkeypatch):
    monkeypatch.setattr(updates, "_get_json", lambda url: release("9.9.9"))
    updates._install["running"] = True
    assert updates.start_update()["started"] is False
