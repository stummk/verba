"""Verba updates the Linux server it runs on: apt update and apt upgrade.

apt does its own job and is not tested here. What is: who is offered this at
all — a Linux server and nothing else —, that a run without the right to
install packages says so instead of failing halfway, that both commands run
in the right order and non-interactively, that every line apt says reaches
the page while it happens, and that a reboot is reported rather than taken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verba import config
from verba.services import osupdate, updates


@pytest.fixture(autouse=True)
def fresh_state():
    """Every test starts without a log and without a running update."""
    osupdate._run.update(
        running=False, detail="", error="", log=[], reboot=False, full=False, finished_at=0.0
    )
    yield


class FakeResult:
    """What procutil.run answers for the `sudo -n true` probe."""

    def __init__(self, returncode: int):
        self.returncode = returncode


class FakeProcess:
    """A finished apt run: its output is there, its exit code is decided."""

    def __init__(self, lines: list[str], code: int = 0):
        self.stdout = iter(f"{line}\n" for line in lines)
        self.code = code
        self.killed = False

    def wait(self) -> int:
        return self.code

    def kill(self) -> None:
        self.killed = True


def fake_apt(monkeypatch, outputs: list[FakeProcess]) -> list[dict]:
    """Answer every spawn with the next prepared process; record the calls."""
    calls: list[dict] = []
    queue = list(outputs)

    def popen(command, **kwargs):
        calls.append({"command": list(command), "env": kwargs.get("env") or {}})
        return queue.pop(0) if queue else FakeProcess([])

    monkeypatch.setattr(osupdate.procutil, "popen", popen)
    return calls


def allow(monkeypatch, elevation: list[str] | None = None) -> None:
    """Make this look like a Linux server that may install packages."""
    monkeypatch.setattr(osupdate, "supported", lambda: True)
    monkeypatch.setattr(osupdate.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(osupdate, "_elevation", lambda: elevation or [])


# ── where the button exists at all ────────────────────────────────────


def test_a_server_installation_on_linux_updates_its_own_packages(monkeypatch):
    monkeypatch.setattr(osupdate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config, "FROZEN", False)
    monkeypatch.setattr(config, "PROJECT_ROOT", Path("/opt/verba"))  # no .git
    assert updates.installation_kind() == updates.SERVER_ZIP
    assert osupdate.supported() is True


def test_windows_has_no_apt(monkeypatch):
    monkeypatch.setattr(osupdate.platform, "system", lambda: "Windows")
    assert osupdate.supported() is False


def test_a_desktop_installation_is_nobodys_server(monkeypatch, tmp_path):
    monkeypatch.setattr(osupdate.platform, "system", lambda: "Linux")

    # the AppImage is a desktop installation even when it serves
    image = tmp_path / "Verba-0.1.1-x86_64.AppImage"
    image.write_bytes(b"\x7fELF")
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(updates.platform, "system", lambda: "Linux")
    monkeypatch.setenv("APPIMAGE", str(image))
    assert updates.installation_kind() == updates.APPIMAGE
    assert osupdate.supported() is False

    # and so is a local app started by a double click
    monkeypatch.setattr(config, "FROZEN", False)
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "1")
    assert osupdate.supported() is False


def test_the_row_is_answered_for_every_installation(monkeypatch):
    """The endpoint always answers; `supported` decides whether it is shown."""
    monkeypatch.setattr(osupdate, "supported", lambda: False)
    info = osupdate.info()
    assert info["supported"] is False
    assert info["can_run"] is False
    assert "Linux-Server" in info["reason"]
    assert info["run"]["log"] == []


# ── the right to install packages ─────────────────────────────────────


def test_root_installs_without_sudo(monkeypatch):
    monkeypatch.setattr(osupdate.os, "geteuid", lambda: 0, raising=False)
    assert osupdate._elevation() == []


def test_a_service_user_needs_passwordless_sudo(monkeypatch):
    monkeypatch.setattr(osupdate.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(osupdate.shutil, "which", lambda name: "/usr/bin/sudo")

    monkeypatch.setattr(
        osupdate.procutil, "run", lambda command, **kwargs: FakeResult(returncode=0)
    )
    assert osupdate._elevation() == ["sudo", "-n"]

    # sudo that wants a password is no use: there is nobody to type it
    monkeypatch.setattr(
        osupdate.procutil, "run", lambda command, **kwargs: FakeResult(returncode=1)
    )
    assert osupdate._elevation() is None


def test_a_server_that_cannot_install_says_why(monkeypatch):
    monkeypatch.setattr(osupdate, "supported", lambda: True)
    monkeypatch.setattr(osupdate.shutil, "which", lambda name: None)
    ok, reason = osupdate.ready()
    assert ok is False
    assert "apt" in reason

    monkeypatch.setattr(osupdate.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(osupdate, "_elevation", lambda: None)
    ok, reason = osupdate.ready()
    assert ok is False
    assert "sudo" in reason


# ── the update itself ─────────────────────────────────────────────────


def test_both_commands_run_in_order_and_without_asking(monkeypatch):
    allow(monkeypatch)
    calls = fake_apt(monkeypatch, [FakeProcess(["Reading package lists... Done"]), FakeProcess([])])

    osupdate._upgrade()

    assert [call["command"] for call in calls] == [
        ["apt-get", "update"],
        [
            "apt-get",
            "--yes",
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
            "upgrade",
        ],
    ]
    # nothing may wait for an answer, and the log is read by an administrator
    for call in calls:
        assert call["env"]["DEBIAN_FRONTEND"] == "noninteractive"
        assert call["env"]["LC_ALL"] == "C"
    assert osupdate.state()["error"] == ""
    assert osupdate.state()["running"] is False


def test_the_thorough_upgrade_has_to_be_asked_for(monkeypatch):
    """dist-upgrade and autoremove may remove packages — never by default."""
    allow(monkeypatch)
    calls = fake_apt(monkeypatch, [FakeProcess([]), FakeProcess([]), FakeProcess([])])

    osupdate._upgrade(full=True)

    assert [call["command"][1:] for call in calls] == [
        ["update"],
        [
            "--yes",
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
            "dist-upgrade",
        ],
        ["--yes", "autoremove"],
    ]
    # the log names what ran, so the record says which of the two it was
    log = " ".join(osupdate.state()["log"])
    assert "dist-upgrade" in log and "autoremove" in log


def test_nothing_is_removed_unless_the_box_was_ticked(monkeypatch):
    allow(monkeypatch)
    calls = fake_apt(monkeypatch, [FakeProcess([]), FakeProcess([])])

    osupdate._upgrade()

    assert [call["command"][-1] for call in calls] == ["update", "upgrade"]
    assert "autoremove" not in " ".join(osupdate.state()["log"])


def test_a_failed_dist_upgrade_removes_nothing_afterwards(monkeypatch):
    allow(monkeypatch)
    calls = fake_apt(
        monkeypatch,
        [FakeProcess([]), FakeProcess(["E: Unmet dependencies"], code=100)],
    )

    osupdate._upgrade(full=True)

    assert [call["command"][-1] for call in calls] == ["update", "dist-upgrade"]
    assert "100" in osupdate.state()["error"]


def test_sudo_is_put_in_front_of_both_commands(monkeypatch):
    allow(monkeypatch, elevation=["sudo", "-n"])
    calls = fake_apt(monkeypatch, [FakeProcess([]), FakeProcess([])])

    osupdate._upgrade()

    assert all(call["command"][:2] == ["sudo", "-n"] for call in calls)


def test_every_line_apt_says_reaches_the_page_while_it_runs(monkeypatch):
    """The whole point of the button: watching what happens on the machine."""
    allow(monkeypatch)
    fake_apt(
        monkeypatch,
        [
            FakeProcess(["Get:1 http://deb.debian.org bookworm InRelease", "Fetched 120 kB"]),
            FakeProcess(["Setting up libssl3 (3.0.15-1) ...\r", ""]),
        ],
    )
    published: list[dict] = []
    monkeypatch.setattr(
        osupdate.hub,
        "publish",
        lambda event, payload: published.append({"event": event, **payload}),
    )

    osupdate._upgrade()

    assert {entry["event"] for entry in published} == {"system.upgrade"}
    # every step carries the whole log, and it only grows
    lengths = [len(entry["log"]) for entry in published]
    assert lengths == sorted(lengths)
    assert osupdate.state()["log"] == [
        "apt-get update — die Paketlisten werden gelesen",
        "Get:1 http://deb.debian.org bookworm InRelease",
        "Fetched 120 kB",
        "apt-get upgrade — die Pakete werden installiert",
        "Setting up libssl3 (3.0.15-1) ...",
        "Fertig — die Systempakete sind aktuell.",
    ]
    assert published[-1]["state"] == "done"


def test_a_failed_package_list_stops_before_anything_is_installed(monkeypatch):
    allow(monkeypatch)
    calls = fake_apt(monkeypatch, [FakeProcess(["E: Could not resolve host"], code=100)])

    osupdate._upgrade()

    assert [call["command"][-1] for call in calls] == ["update"]
    run = osupdate.state()
    assert run["running"] is False
    assert "100" in run["error"]
    # apt said why in its own words, and that is in the log
    assert "E: Could not resolve host" in run["log"]


def test_a_required_reboot_is_reported_not_performed(monkeypatch, tmp_path):
    allow(monkeypatch)
    calls = fake_apt(monkeypatch, [FakeProcess([]), FakeProcess(["Setting up linux-image ..."])])
    marker = tmp_path / "reboot-required"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(osupdate, "REBOOT_MARKER", marker)

    osupdate._upgrade()

    run = osupdate.state()
    assert run["reboot"] is True
    assert "neu gestartet" in run["log"][-1]
    # the two apt calls are all that ran — a reboot is the administrator's
    assert [call["command"][-1] for call in calls] == ["update", "upgrade"]


def test_a_missing_apt_is_a_message_not_a_traceback(monkeypatch):
    allow(monkeypatch)

    def popen(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(osupdate.procutil, "popen", popen)

    osupdate._upgrade()

    assert "apt-get" in osupdate.state()["error"]


def test_the_log_is_never_written_to_disk(monkeypatch):
    """It is the record of one watched run, not a file somebody has to clean."""
    allow(monkeypatch)
    fake_apt(monkeypatch, [FakeProcess(["Fetched 120 kB"]), FakeProcess([])])
    data = config.base_data_dir()
    before = {path for path in data.rglob("*")} if data.exists() else set()

    osupdate._upgrade()

    after = {path for path in data.rglob("*")} if data.exists() else set()
    assert after == before
    assert osupdate.state()["log"]  # it exists — in this process only


def test_the_log_cannot_grow_without_end(monkeypatch):
    allow(monkeypatch)
    fake_apt(monkeypatch, [FakeProcess([f"Unpacking package-{index}" for index in range(900)])])

    osupdate._upgrade()

    assert len(osupdate.state()["log"]) == osupdate._LOG_LINES


# ── starting it ───────────────────────────────────────────────────────


def test_a_server_that_may_not_install_never_starts(monkeypatch):
    monkeypatch.setattr(osupdate, "ready", lambda: (False, "Kein apt"))
    assert osupdate.start() == {"started": False, "reason": "Kein apt"}
    assert osupdate.state()["running"] is False


def test_a_second_click_does_not_start_a_second_apt(monkeypatch):
    monkeypatch.setattr(osupdate, "ready", lambda: (True, ""))
    osupdate._run["running"] = True
    result = osupdate.start()
    assert result["started"] is False
    assert "läuft bereits" in result["reason"]
