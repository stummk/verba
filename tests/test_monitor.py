"""The live resource reading behind the bars in the system settings."""

from __future__ import annotations

import pytest

from verba.services import hardware, monitor


@pytest.fixture(autouse=True)
def _clean_monitor():
    monitor.invalidate()
    monitor.reset_cpu_baseline()
    yield
    monitor.invalidate()
    monitor.reset_cpu_baseline()


def test_the_reading_has_every_bar_the_ui_draws():
    data = monitor.sample(fresh=True)
    assert set(data) == {
        "cpu_percent",
        "cpu_cores",
        "ram_total_mb",
        "ram_used_mb",
        "gpu_name",
        "gpu_percent",
        "vram_total_mb",
        "vram_used_mb",
    }
    assert data["ram_total_mb"] >= data["ram_used_mb"] >= 0
    assert data["vram_total_mb"] >= data["vram_used_mb"] >= 0


def test_the_processor_load_is_a_percentage_on_this_platform():
    """Windows and Linux each have their own counter — both must answer.

    A platform whose counter cannot be read falls back to the load average,
    which may be `None` on a machine that has neither; a *number* outside
    0–100 would be the real defect.
    """
    load = monitor.sample(fresh=True)["cpu_percent"]
    assert load is None or 0.0 <= load <= 100.0


def test_the_first_reading_measures_its_own_window(monkeypatch):
    """No predecessor to subtract — so it waits a moment and reads twice."""
    readings = iter([(100.0, 200.0), (150.0, 300.0)])
    monkeypatch.setattr(monitor, "_cpu_times", lambda: next(readings))
    monkeypatch.setattr(monitor.time, "sleep", lambda _: None)
    assert monitor.cpu_percent() == 50.0


def test_a_busy_processor_is_not_reported_as_idle(monkeypatch):
    """A window in which nothing was idle is 100 %, not 0."""
    readings = iter([(100.0, 200.0), (150.0, 300.0), (250.0, 400.0)])
    monkeypatch.setattr(monitor, "_cpu_times", lambda: next(readings))
    monkeypatch.setattr(monitor.time, "sleep", lambda _: None)
    monitor.cpu_percent()  # the first call leaves (150, 300) as the baseline
    assert monitor.cpu_percent() == 100.0


def test_two_readings_within_the_same_tick_report_nothing(monkeypatch):
    """No time passed — a division would be by zero, an answer a lie."""
    monkeypatch.setattr(monitor, "_cpu_times", lambda: (100.0, 200.0))
    monkeypatch.setattr(monitor.time, "sleep", lambda _: None)
    assert monitor.cpu_percent() is None


def test_used_memory_is_what_is_gone_not_what_is_left(monkeypatch):
    monkeypatch.setattr(hardware, "ram_mb", lambda: (16000, 6000))
    monkeypatch.setattr(
        hardware,
        "gpu_info",
        lambda: {
            "name": "NVIDIA RTX A500 Laptop GPU",
            "vram_total_mb": 4096,
            "vram_free_mb": 1096,
            "util_percent": 42.0,
        },
    )
    data = monitor.sample(fresh=True)
    assert data["ram_used_mb"] == 10000
    assert data["vram_used_mb"] == 3000
    assert data["gpu_percent"] == 42.0
    assert data["gpu_name"] == "NVIDIA RTX A500 Laptop GPU"


def test_a_machine_without_a_graphics_card_reports_no_vram(monkeypatch):
    """The UI leaves the two GPU bars out — an empty bar would claim idle."""
    monkeypatch.setattr(
        hardware,
        "gpu_info",
        lambda: {"name": "", "vram_total_mb": 0, "vram_free_mb": 0, "util_percent": None},
    )
    data = monitor.sample(fresh=True)
    assert data["vram_total_mb"] == 0
    assert data["gpu_percent"] is None


def test_a_second_reader_does_not_spawn_a_second_probe(monkeypatch):
    """Several open settings tabs must not multiply nvidia-smi."""
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {"name": "", "vram_total_mb": 0, "vram_free_mb": 0, "util_percent": None}

    monkeypatch.setattr(hardware, "gpu_info", counted)
    monitor.sample(fresh=True)
    for _ in range(5):
        monitor.sample()
    assert calls["n"] == 1


def test_the_endpoint_answers_the_bars(client):
    response = client.get("/api/system/resources")
    assert response.status_code == 200
    data = response.json()
    assert data["cpu_cores"] >= 1
    assert data["ram_total_mb"] >= data["ram_used_mb"] >= 0
