from __future__ import annotations

from verba import procutil, setup_check


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_system_status_shape(client):
    response = client.get("/api/system/status")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["ready"], bool)
    assert isinstance(data["checks"], list)
    check_ids = {check["id"] for check in data["checks"]}
    assert {"python", "ffmpeg", "gpu"} <= check_ids
    assert any(check_id.startswith("group:") for check_id in check_ids)


def test_python_check_passes():
    result = setup_check.check_python()
    assert result.ok is True


def test_ffmpeg_check_uses_tools_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = setup_check.check_ffmpeg()
    assert result.ok is False

    # a binary inside <data>/tools is found
    from verba import config

    exe = "ffmpeg.exe" if setup_check.platform.system() == "Windows" else "ffmpeg"
    fake = config.tools_dir() / "ffmpeg" / "bin" / exe
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"")
    assert setup_check.check_ffmpeg().ok is True


def test_system_info_shape(client):
    from verba import __version__

    response = client.get("/api/system/info")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == __version__
    assert data["os"]
    assert data["python"]
    assert data["cpu_cores"] >= 1
    assert data["ram_total_mb"] >= data["ram_available_mb"] >= 0
    assert set(data["gpu"]) == {"name", "vram_total_mb", "vram_free_mb"}
    assert isinstance(data["ffmpeg"], bool)


def test_setup_run_endpoint_reports_started(client, monkeypatch):
    calls = {}

    def fake_run_setup(include_optional=True):
        calls["include_optional"] = include_optional

    monkeypatch.setattr(setup_check, "run_setup", fake_run_setup)
    response = client.post("/api/system/setup/run", json={"include_optional": False})
    assert response.status_code == 200
    assert response.json()["started"] is True


def test_feature_install_bootstraps_missing_pip(monkeypatch):
    calls = []

    monkeypatch.setattr(setup_check.config, "FROZEN", False)
    monkeypatch.setattr(setup_check, "find_spec", lambda name: None if name == "pip" else object())
    monkeypatch.setattr(
        setup_check.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    monkeypatch.setattr(setup_check, "_run_child", lambda command, on_line: (0, []))

    setup_check._pip_install(["example"], "Testgruppe")

    assert calls == [
        (
            [setup_check.sys.executable, "-m", "ensurepip", "--upgrade"],
            {
                "check": True,
                "capture_output": True,
                "text": True,
                # no console window flashes over the desktop on Windows
                "creationflags": procutil.NO_WINDOW,
            },
        )
    ]


def test_frozen_build_never_calls_ensurepip(monkeypatch):
    """The bundled pip is always there; `python -m ensurepip` does not exist."""
    monkeypatch.setattr(setup_check.config, "FROZEN", True)
    monkeypatch.setattr(setup_check, "find_spec", lambda name: None)

    def fail(*args, **kwargs):
        raise AssertionError("ensurepip must not run in a frozen build")

    monkeypatch.setattr(setup_check.subprocess, "run", fail)
    monkeypatch.setattr(setup_check, "_run_child", lambda command, on_line: (0, []))
    setup_check._pip_install(["example"], "Testgruppe")


def test_shutdown_endpoint_is_disabled_outside_desktop_mode(client, monkeypatch):
    monkeypatch.delenv("VERBA_DESKTOP_MODE", raising=False)
    response = client.post("/api/system/shutdown")
    assert response.status_code == 200
    assert response.json() == {"stopped": False}
