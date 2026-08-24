from __future__ import annotations

import pytest

from verba.services import llamacpp


def test_recommendation_scales_with_hardware():
    tiny = llamacpp.recommend_model({"ram_mb": 4000, "vram_mb": 0, "gpu_name": ""})
    assert tiny["name"] == "Qwen3-1.7B-Q8_0"

    mid = llamacpp.recommend_model({"ram_mb": 16000, "vram_mb": 0, "gpu_name": ""})
    assert mid["name"] == "Qwen3-4B-Q4_K_M"

    gpu = llamacpp.recommend_model({"ram_mb": 16000, "vram_mb": 12000, "gpu_name": "RTX"})
    assert gpu["name"] == "Qwen3-8B-Q4_K_M"


def test_probe_hardware_returns_numbers():
    hw = llamacpp.probe_hardware()
    assert hw["ram_mb"] > 0  # every dev/CI machine has RAM
    assert hw["vram_mb"] >= 0


def test_delete_model_rejects_traversal():
    with pytest.raises(ValueError):
        llamacpp.delete_model("../app.db")
    with pytest.raises(ValueError):
        llamacpp.delete_model("model.bin")  # only .gguf is allowed


def test_llm_status_endpoint(client):
    data = client.get("/api/models/llm").json()
    assert data["binary_installed"] is False
    assert data["server_running"] is False
    assert len(data["catalog"]) == 3
    assert data["recommended"]["name"] in {m["name"] for m in data["catalog"]}
    assert data["installed"] == []


def test_llm_download_unknown_model_rejected(client):
    response = client.post("/api/models/llm/download", json={"name": "evil/../model"})
    assert response.status_code == 422


def test_process_requires_configured_llm(client, tmp_path):
    project = client.post("/api/projects", json={"name": "P"}).json()
    response = client.post(f"/api/projects/{project['id']}/process", json={"steps": ["cleanup"]})
    assert response.status_code == 409  # llm mode is "none" by default


def test_queue_endpoint(client):
    data = client.get("/api/jobs/queue").json()
    assert data["llm_location"] == "none"
    assert data["lanes"] == {"main": [], "llm": []}
