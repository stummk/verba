"""The CUDA libraries the transcription needs: verdict, wiring and installation.

Nothing here touches a real GPU or a real pip run. What is tested is the
reasoning around them — when the component applies at all, which of the three
states the checklist reports, that a broken driver is never answered with a
gigabyte of downloads, and that installing the libraries lifts whisper's
permanent CPU fallback without a restart.
"""

from __future__ import annotations

import sys

import pytest

from verba import config, setup_check
from verba.services import cudalibs, whisper

#: conftest patches `applies` away for every test so no run can trigger the
#: real installation — captured here, before that happens, for the four tests
#: that are about the predicate itself.
REAL_APPLIES = cudalibs.applies


@pytest.fixture()
def real_applies(monkeypatch):
    monkeypatch.setattr(cudalibs, "applies", REAL_APPLIES)


@pytest.fixture(autouse=True)
def fresh_state():
    cudalibs.invalidate()
    yield
    cudalibs.invalidate()


def with_gpu(monkeypatch, *, libraries: bool, driver: bool, missing: str = "libcudnn.so.9") -> None:
    """A machine with a GPU whose libraries and driver are in a given state."""
    monkeypatch.setattr(cudalibs, "applies", lambda: True)
    monkeypatch.setattr(cudalibs, "preload", lambda: [])
    monkeypatch.setattr(
        cudalibs, "_libraries_present", lambda: (libraries, "" if libraries else missing)
    )
    monkeypatch.setattr(
        cudalibs, "driver_state", lambda: (driver, "cuda" if driver else "cuInit → 999")
    )
    cudalibs.invalidate()


# ── when does this component apply? ───────────────────────────────────


def test_no_gpu_means_nothing_to_install(real_applies, monkeypatch):
    monkeypatch.setattr(cudalibs.hardware, "has_gpu", lambda hw=None: False)
    assert cudalibs.applies() is False


def test_a_gpu_this_machine_may_use_wants_the_libraries(real_applies, monkeypatch):
    monkeypatch.setattr(cudalibs.hardware, "has_gpu", lambda hw=None: True)
    assert cudalibs.applies() is True


def test_the_cpu_setting_is_respected(real_applies, monkeypatch):
    """Whoever sends the transcription to the CPU is not offered a GPU download."""
    monkeypatch.setattr(cudalibs.hardware, "has_gpu", lambda hw=None: True)
    settings = config.get_settings()
    settings.whisper.device = "cpu"
    config.save_settings(settings)
    assert cudalibs.applies() is False


def test_macos_has_no_cuda(real_applies, monkeypatch):
    monkeypatch.setattr(cudalibs.hardware, "has_gpu", lambda hw=None: True)
    monkeypatch.setattr(cudalibs.platform, "system", lambda: "Darwin")
    assert cudalibs.applies() is False


# ── the three states ──────────────────────────────────────────────────


def test_working_libraries_need_no_installation(monkeypatch):
    with_gpu(monkeypatch, libraries=True, driver=True)
    state = cudalibs.state()
    assert state["ok"] is True
    assert state["installable"] is False


def test_missing_libraries_are_offered(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    state = cudalibs.state()
    assert state["ok"] is False
    assert state["installable"] is True
    assert "libcudnn.so.9" in state["detail"]


def test_a_silent_driver_is_not_answered_with_a_download(monkeypatch):
    """The LXC case: nvidia-smi lists the card, every CUDA call fails.

    Installing a gigabyte of libraries would change nothing there, so the row
    explains where to look instead of offering it.
    """
    with_gpu(monkeypatch, libraries=False, driver=False)
    state = cudalibs.state()
    assert state["ok"] is False
    assert state["installable"] is False
    assert "/dev/nvidia-uvm" in state["detail"]


def test_the_verdict_is_cached_until_it_is_dropped(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    assert cudalibs.state()["ok"] is False
    monkeypatch.setattr(cudalibs, "_libraries_present", lambda: (True, ""))
    assert cudalibs.state()["ok"] is False, "the cached verdict must be reused"
    assert cudalibs.state(refresh=True)["ok"] is True


# ── finding and preloading the wheels ─────────────────────────────────


def _wheel_tree(root, name: str) -> None:
    subdir = "bin" if cudalibs._WINDOWS else "lib"
    directory = root / "nvidia" / name / subdir
    directory.mkdir(parents=True)
    (directory / ("cublas64_12.dll" if cudalibs._WINDOWS else "libcublas.so.12")).write_bytes(b"")


def test_library_dirs_finds_the_wheels_on_sys_path(tmp_path, monkeypatch):
    _wheel_tree(tmp_path, "cublas")
    _wheel_tree(tmp_path, "cudnn")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    names = [directory.parent.name for directory in cudalibs.library_dirs()]
    assert names == ["cublas", "cudnn"]


def test_an_empty_wheel_directory_is_not_a_wheel(tmp_path, monkeypatch):
    (tmp_path / "nvidia" / "cublas" / ("bin" if cudalibs._WINDOWS else "lib")).mkdir(parents=True)
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    assert cudalibs.library_dirs() == []


def test_preload_handles_every_directory_once(tmp_path, monkeypatch):
    _wheel_tree(tmp_path, "cublas")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])
    monkeypatch.setattr(cudalibs, "_preloaded_dirs", [])
    monkeypatch.setattr(cudalibs, "_load_all", lambda directory: None)
    monkeypatch.setattr(cudalibs, "_WINDOWS", False)  # no real DLL directory in a test
    assert len(cudalibs.preload()) == 1
    assert cudalibs.preload() == [], "a second call has nothing left to do"


# ── the checklist and the setup steps ─────────────────────────────────


def test_the_checklist_stays_quiet_without_a_gpu():
    ids = [check.id for check in setup_check._all_checks()]
    assert "cuda" not in ids


def test_the_checklist_names_the_component_on_a_gpu_machine(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    checks = {check.id: check for check in setup_check._all_checks()}
    assert "cuda" in checks
    row = checks["cuda"]
    assert row.required is False, "the transcription still runs on the CPU"
    assert row.installable is True


def test_the_setup_installs_the_libraries(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    monkeypatch.setattr(setup_check, "check_ffmpeg", lambda: _ok_check())
    monkeypatch.setattr(setup_check, "group_installed", lambda group: True)
    labels = [label for label, _ in setup_check._pending_steps(include_optional=True)]
    assert labels == [cudalibs.LABEL]


def test_the_essentials_only_run_leaves_the_gigabyte_alone(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    monkeypatch.setattr(setup_check, "check_ffmpeg", lambda: _ok_check())
    monkeypatch.setattr(setup_check, "group_installed", lambda group: True)
    assert setup_check._pending_steps(include_optional=False) == []


def test_nothing_is_installed_for_a_silent_driver(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=False)
    monkeypatch.setattr(setup_check, "check_ffmpeg", lambda: _ok_check())
    monkeypatch.setattr(setup_check, "group_installed", lambda group: True)
    assert setup_check._pending_steps(include_optional=True) == []


# ── the installation itself ───────────────────────────────────────────


def test_libraries_that_do_not_help_are_not_a_failed_installation(monkeypatch):
    """pip did its job; that the GPU stays silent is a different problem.

    Raising here would abort the whole setup run and mark it failed — over a
    machine whose transcription keeps working on the CPU.
    """
    with_gpu(monkeypatch, libraries=True, driver=False)
    monkeypatch.setattr(setup_check, "_pip_install", lambda packages, step: None)
    monkeypatch.setattr(setup_check.hub, "publish", lambda event_type, data=None: None)
    setup_check.install_cuda_libs()  # does not raise


def test_a_failed_pip_run_fails_the_step(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)

    def explode(packages, step):
        raise RuntimeError("kein Netz")

    monkeypatch.setattr(setup_check, "_pip_install", explode)
    monkeypatch.setattr(setup_check.hub, "publish", lambda event_type, data=None: None)
    with pytest.raises(RuntimeError, match="kein Netz"):
        setup_check.install_cuda_libs()


def test_installing_only_the_libraries_does_not_finish_the_wizard(monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    monkeypatch.setattr(setup_check, "_pip_install", lambda packages, step: None)
    monkeypatch.setattr(setup_check.hub, "publish", lambda event_type, data=None: None)

    setup_check.run_cuda_libs()

    assert setup_check.progress.error == ""
    assert config.get_settings().setup.completed is False


# ── whisper's memory of a broken CUDA ─────────────────────────────────


def test_an_installation_lifts_the_permanent_cpu_fallback(monkeypatch):
    """`_cuda_broken` survives for the process — but not the fix for it."""
    monkeypatch.setattr(whisper, "_cuda_broken", True)
    monkeypatch.setattr(whisper, "_cuda_generation", cudalibs.generation())
    assert whisper._cuda_available() is False

    cudalibs.mark_installed()

    assert whisper._cuda_available() is True


# ── the endpoint ──────────────────────────────────────────────────────


def test_the_button_starts_the_installation(client, monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    started: list[str] = []
    monkeypatch.setattr(setup_check, "run_cuda_libs", lambda: started.append("run"))
    response = client.post("/api/system/cuda-libs")
    assert response.status_code == 200
    assert response.json()["started"] is True


def test_the_button_refuses_what_it_cannot_fix(client, monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=False)
    response = client.post("/api/system/cuda-libs")
    assert response.json()["started"] is False
    assert "/dev/nvidia-uvm" in response.json()["reason"]


def test_the_settings_page_learns_about_the_libraries(client, monkeypatch):
    with_gpu(monkeypatch, libraries=False, driver=True)
    info = client.get("/api/system/info").json()
    assert info["cuda"]["applies"] is True
    assert info["cuda"]["installable"] is True


def _ok_check() -> setup_check.CheckResult:
    return setup_check.CheckResult(
        id="ffmpeg", label="ffmpeg", ok=True, required=True, installable=True
    )
