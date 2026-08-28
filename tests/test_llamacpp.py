from __future__ import annotations

from pathlib import Path

import pytest

from verba import config
from verba.services import llamacpp


def machine(ram_mb: int, vram_mb: int = 0, gpu: str = "") -> dict:
    """A probe result as services/hardware.py reports it (see probe())."""
    return {
        "ram_total_mb": ram_mb,
        "ram_available_mb": ram_mb,
        "gpu_name": gpu,
        "vram_total_mb": vram_mb,
        "vram_free_mb": vram_mb,
    }


def test_recommendation_scales_with_hardware():
    tiny = llamacpp.recommend_model(machine(4000))
    assert tiny["name"] == "Qwen3-1.7B-Q8_0"

    mid = llamacpp.recommend_model(machine(16000))
    assert mid["name"] == "Qwen3-4B-Q4_K_M"

    gpu = llamacpp.recommend_model(machine(16000, vram_mb=12000, gpu="RTX"))
    assert gpu["name"] == "Qwen3-8B-Q4_K_M"


def test_the_recommendation_ignores_unflagged_alternatives():
    """An alternative of the same size must not change what new users get."""
    flagged = [m for m in llamacpp.MODEL_CATALOG if m.get("recommended")]
    alternatives = [m for m in llamacpp.MODEL_CATALOG if not m.get("recommended")]
    assert flagged and alternatives, "the catalog offers alternatives besides the recommended line"
    # a machine that fits everything still gets a recommended line, not the
    # largest alternative that happens to sit last in the list
    everything = llamacpp.recommend_model(machine(64000, vram_mb=48000, gpu="A6000"))
    assert everything.get("recommended") is True
    assert everything["name"] == flagged[-1]["name"]


def test_the_catalog_is_ordered_and_reachable():
    """Ordered by hardware need, and no duplicate names or files."""
    needs = [entry["min_free_mb"] for entry in llamacpp.MODEL_CATALOG]
    assert needs == sorted(needs)
    names = [entry["name"] for entry in llamacpp.MODEL_CATALOG]
    files = [entry["file"] for entry in llamacpp.MODEL_CATALOG]
    assert len(set(names)) == len(names)
    assert len(set(files)) == len(files)
    for entry in llamacpp.MODEL_CATALOG:
        assert entry["url"].startswith("https://huggingface.co/")
        assert entry["url"].endswith(".gguf")
        # gated repositories answer 401 without a token — those cannot be used
        assert "/google/gemma" not in entry["url"]


def test_alternatives_from_another_family_are_offered():
    families = {entry["name"].split("-")[0].lower() for entry in llamacpp.MODEL_CATALOG}
    assert {"qwen3", "gemma"} <= families


def test_probe_hardware_returns_numbers():
    hw = llamacpp.probe_hardware()
    assert hw["ram_total_mb"] > 0  # every dev/CI machine has RAM
    assert hw["ram_total_mb"] >= hw["ram_available_mb"] >= 0
    assert hw["vram_total_mb"] >= 0


def test_delete_model_rejects_traversal():
    with pytest.raises(ValueError):
        llamacpp.delete_model("../app.db")
    with pytest.raises(ValueError):
        llamacpp.delete_model("model.bin")  # only .gguf is allowed


def test_llm_status_endpoint(client):
    data = client.get("/api/models/llm").json()
    assert data["binary_installed"] is False
    assert data["server_running"] is False
    assert len(data["catalog"]) == len(llamacpp.MODEL_CATALOG)
    assert data["recommended"]["name"] in {m["name"] for m in data["catalog"]}
    assert data["installed"] == []
    assert data["models_dir"].endswith("llm")


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


# ── the models directory ──────────────────────────────────────────────


def test_a_configured_directory_is_used_as_is(tmp_path):
    """An existing GGUF collection is used from where it is — no copying."""
    collection = tmp_path / "F" / "Models" / "llm"
    collection.mkdir(parents=True)
    (collection / "mein-modell-Q4_K_M.gguf").write_bytes(b"gguf")

    settings = config.get_settings()
    settings.llm.models_dir = str(collection)
    config.save_settings(settings)

    assert llamacpp.llm_models_dir() == collection
    assert [m["file"] for m in llamacpp.list_installed_models()] == ["mein-modell-Q4_K_M.gguf"]
    assert llamacpp._pick_model_file() == collection / "mein-modell-Q4_K_M.gguf"
    # nothing was copied into the default directory
    assert not list(config.default_llm_models_dir().glob("*.gguf"))


def test_the_configured_model_is_taken_from_that_directory(tmp_path):
    collection = tmp_path / "gguf"
    collection.mkdir()
    for name in ("a-Q4.gguf", "b-Q4.gguf"):
        (collection / name).write_bytes(b"gguf")

    settings = config.get_settings()
    settings.llm.models_dir = str(collection)
    settings.llm.model = "b-Q4.gguf"
    config.save_settings(settings)

    assert llamacpp._pick_model_file() == collection / "b-Q4.gguf"


def test_a_relative_directory_is_stored_absolute():
    settings = config.Settings.model_validate({"llm": {"models_dir": "modelle"}})
    assert Path(settings.llm.models_dir).is_absolute()


def test_delete_stays_inside_the_configured_directory(tmp_path):
    """A traversal attempt is stripped to a plain name, never followed."""
    collection = tmp_path / "gguf"
    collection.mkdir()
    outsider = tmp_path / "elsewhere.gguf"
    outsider.write_bytes(b"gguf")
    settings = config.get_settings()
    settings.llm.models_dir = str(collection)
    config.save_settings(settings)

    llamacpp.delete_model("../elsewhere.gguf")

    assert outsider.is_file(), "the file outside the models directory must survive"
    with pytest.raises(ValueError):
        llamacpp.delete_model("../app.db")  # only .gguf at all
