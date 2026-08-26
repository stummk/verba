"""First-run setup: progress reporting, live checklist and process isolation.

The pip installation runs in a child process and no feature-group module is
ever imported here — on Windows a loaded extension module is locked, which is
what used to break the installation of a later group (numpy under
sentence-transformers).
"""

from __future__ import annotations

import sys

from verba import config, setup_check


def collect_events(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(
        setup_check.hub,
        "publish",
        lambda event_type, data=None: events.append(data or {}),
    )
    return events


# ── progress arithmetic ───────────────────────────────────────────────


def test_percent_spans_the_whole_setup():
    progress = setup_check.SetupProgress(total_steps=4)
    assert progress.overall_percent(0) == 0
    assert progress.overall_percent(50) == 12  # half of the first of four steps
    progress.step_index = 2
    assert progress.overall_percent(0) == 50
    assert progress.overall_percent(100) == 75
    progress.step_index = 4
    assert progress.overall_percent(100) == 100


def test_percent_never_exceeds_one_hundred():
    progress = setup_check.SetupProgress(total_steps=1, step_index=1)
    assert progress.overall_percent(500) == 100


def test_progress_never_moves_backwards_between_steps(monkeypatch):
    events = collect_events(monkeypatch)
    monkeypatch.setattr(setup_check, "check_ffmpeg", lambda: _ok_check())
    monkeypatch.setattr(setup_check, "group_installed", lambda group: False)
    monkeypatch.setattr(setup_check, "_pip_install", lambda packages, step: None)
    monkeypatch.setattr(setup_check, "_verify_import", lambda group: None)

    setup_check.run_setup(include_optional=True)

    percentages = [event["percent"] for event in events]
    assert percentages == sorted(percentages), percentages
    assert percentages[-1] == 100


# ── live checklist ────────────────────────────────────────────────────


def test_every_finished_component_updates_the_checklist(monkeypatch):
    """The wizard ticks components off while the setup runs, so each event
    carries the checklist that belongs to it."""
    events = collect_events(monkeypatch)
    installed: set[str] = set()
    monkeypatch.setattr(setup_check, "check_ffmpeg", lambda: _ok_check())
    monkeypatch.setattr(setup_check, "group_installed", lambda group: group.key in installed)
    monkeypatch.setattr(
        setup_check, "_pip_install", lambda packages, step: installed.add(_key_for(step))
    )
    monkeypatch.setattr(setup_check, "_verify_import", lambda group: None)

    setup_check.run_setup(include_optional=True)

    def ticked_groups(event: dict) -> set[str]:
        return {c["id"] for c in event["checks"] if c["ok"] and c["id"].startswith("group")}

    ticked = [ticked_groups(event) for event in events if event["checks"]]
    # grows monotonically and ends with every group installed
    assert all(a <= b for a, b in zip(ticked, ticked[1:], strict=False)), ticked
    assert ticked[-1] == {f"group:{group.key}" for group in setup_check.FEATURE_GROUPS}


def test_status_reports_the_checklist_after_a_failure(monkeypatch):
    events = collect_events(monkeypatch)
    monkeypatch.setattr(setup_check, "check_ffmpeg", lambda: _ok_check())
    monkeypatch.setattr(setup_check, "group_installed", lambda group: False)

    def explode(packages, step):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(setup_check, "_pip_install", explode)

    setup_check.run_setup(include_optional=True)

    assert setup_check.progress.error == "kaputt"
    assert setup_check.progress.running is False
    assert events[-1]["checks"], "the final event must carry the checklist"
    assert not config.get_settings().setup.completed


# ── process isolation ─────────────────────────────────────────────────


def test_group_check_does_not_import_the_module():
    """`group_installed` must locate the module without executing it."""
    for name in ("faster_whisper", "sentence_transformers", "fpdf"):
        sys.modules.pop(name, None)
    for group in setup_check.FEATURE_GROUPS:
        setup_check.group_installed(group)
        assert group.import_name not in sys.modules


def test_group_check_finds_an_installed_module():
    group = setup_check.FeatureGroup(
        key="json", label="JSON", packages=["json"], import_name="json"
    )
    assert setup_check.group_installed(group) is True
    missing = setup_check.FeatureGroup(
        key="nope", label="Nope", packages=["nope"], import_name="verba_does_not_exist"
    )
    assert setup_check.group_installed(missing) is False


def test_a_half_deleted_package_does_not_count_as_installed(tmp_path, monkeypatch):
    """What a locked pip run leaves behind: the directory without the code.

    It resolves as a namespace package, and reporting it as installed would
    skip the group forever."""
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "verba_debris").mkdir()  # no __init__.py — nothing left inside
    setup_check.invalidate_caches()
    group = setup_check.FeatureGroup(
        key="debris", label="Debris", packages=["debris"], import_name="verba_debris"
    )
    assert setup_check.group_installed(group) is False


def test_child_process_output_is_streamed_line_by_line():
    lines: list[str] = []
    code, collected = setup_check._run_child(
        [sys.executable, "-c", "print('Collecting one'); print('Collecting two')"],
        lines.append,
    )
    assert code == 0
    assert lines == ["Collecting one", "Collecting two"] == collected


def test_child_process_failure_is_reported_with_its_output(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", False)
    group = setup_check.FeatureGroup(
        key="broken", label="Kaputte Gruppe", packages=[], import_name="verba_missing_module"
    )
    try:
        setup_check._verify_import(group)
    except RuntimeError as error:
        assert "Kaputte Gruppe" in str(error)
    else:
        raise AssertionError("a failing import must raise")


# ── damaged packages after a locked pip run ───────────────────────────


def test_locked_dependency_is_marked_for_repair(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    output = [
        "PermissionError: [WinError 5] Zugriff verweigert: "
        "'C:\\\\Users\\\\x\\\\AppData\\\\Local\\\\Verba\\\\site-packages\\\\numpy\\\\linalg\\\\"
        "_umath_linalg.cp312-win_amd64.pyd'"
    ]
    monkeypatch.setattr(setup_check, "_run_child", lambda command, on_line: (2, output))

    try:
        setup_check._pip_install(["sentence-transformers>=3.0"], "Semantische Suche")
    except RuntimeError as error:
        assert "neu starten" in str(error)
    else:
        raise AssertionError("a failing pip run must raise")

    assert config.site_packages_repair_marker().read_text(encoding="utf-8") == "numpy"


def test_unrelated_failure_marks_nothing_for_repair(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(
        setup_check,
        "_run_child",
        lambda command, on_line: (1, ["ERROR: No matching distribution found for nope"]),
    )
    try:
        setup_check._pip_install(["nope"], "Testgruppe")
    except RuntimeError as error:
        assert "neu starten" not in str(error)
    assert not config.site_packages_repair_marker().exists()


def test_damaged_packages_parses_posix_paths():
    output = ["PermissionError: '/home/u/.local/share/verba/site-packages/numpy/linalg/x.so'"]
    assert setup_check._damaged_packages(output) == ["numpy"]


# ── helpers ───────────────────────────────────────────────────────────


def _ok_check() -> setup_check.CheckResult:
    return setup_check.CheckResult(
        id="ffmpeg", label="ffmpeg", ok=True, required=True, installable=True
    )


def _missing_check() -> setup_check.CheckResult:
    return setup_check.CheckResult(
        id="ffmpeg", label="ffmpeg", ok=False, required=True, installable=True
    )


def _key_for(label: str) -> str:
    for group in setup_check.FEATURE_GROUPS:
        if group.label == label:
            return group.key
    raise AssertionError(f"unknown group label: {label}")


# ── finishing the wizard ──────────────────────────────────────────────


def test_the_wizard_can_finish_with_steps_skipped(client):
    """Every step is skippable, so "completed" means the user reached the end."""
    assert client.get("/api/system/status").json()["setup_completed"] is False

    status = client.post("/api/system/setup/complete").json()

    assert status["setup_completed"] is True
    assert client.get("/api/system/status").json()["setup_completed"] is True
    assert config.get_settings().setup.completed is True


def test_completing_does_not_claim_components_are_installed(client, monkeypatch):
    monkeypatch.setattr(setup_check, "check_ffmpeg", lambda: _missing_check())

    status = client.post("/api/system/setup/complete").json()

    assert status["setup_completed"] is True
    assert status["ready"] is False  # honest about the skipped installation


def test_only_finishing_completes_the_setup(client):
    """Postponing the wizard keeps the reminder — nothing is written."""
    client.get("/api/system/status")
    assert config.get_settings().setup.completed is False
