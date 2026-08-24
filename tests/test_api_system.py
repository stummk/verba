from __future__ import annotations

from verba import setup_check


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


def test_gpu_info_parses_nvidia_smi(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "NVIDIA RTX A500 Laptop GPU, 4096, 3210\n"

    monkeypatch.setattr(setup_check.subprocess, "run", lambda *a, **k: FakeResult())
    info = setup_check._gpu_info()
    assert info == {
        "name": "NVIDIA RTX A500 Laptop GPU",
        "vram_total_mb": 4096,
        "vram_free_mb": 3210,
    }


def test_gpu_info_without_nvidia_smi(monkeypatch):
    def raise_missing(*args, **kwargs):
        raise OSError("not found")

    monkeypatch.setattr(setup_check.subprocess, "run", raise_missing)
    info = setup_check._gpu_info()
    assert info == {"name": "", "vram_total_mb": 0, "vram_free_mb": 0}


def test_memory_probe_returns_plausible_values():
    total, available = setup_check._memory_mb()
    assert total >= available >= 0


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

    class FakeProcess:
        stdout = iter(["Successfully installed example\n"])
        returncode = 0

        def wait(self):
            return 0

    monkeypatch.setattr(setup_check, "find_spec", lambda name: None if name == "pip" else object())
    monkeypatch.setattr(
        setup_check.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    monkeypatch.setattr(setup_check.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    setup_check._pip_install_subprocess(["example"], lambda line: None)

    assert calls == [
        (
            [setup_check.sys.executable, "-m", "ensurepip", "--upgrade"],
            {"check": True, "capture_output": True, "text": True},
        )
    ]


def test_shutdown_endpoint_is_disabled_outside_desktop_mode(client, monkeypatch):
    monkeypatch.delenv("VERBA_DESKTOP_MODE", raising=False)
    response = client.post("/api/system/shutdown")
    assert response.status_code == 200
    assert response.json() == {"stopped": False}
