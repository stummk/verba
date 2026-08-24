from __future__ import annotations

from verba import config
from verba.api.settings import API_KEY_MASK


def test_get_settings_shape(client):
    response = client.get("/api/settings")
    assert response.status_code == 200
    data = response.json()
    assert data["server"]["port"] == 8710
    assert data["llm"]["mode"] == "none"


def test_update_settings_persists(client):
    data = client.get("/api/settings").json()
    data["whisper"]["model"] = "medium"
    data["logging"]["retention_days"] = 7

    response = client.put("/api/settings", json=data)
    assert response.status_code == 200

    config.reset_cache()
    assert config.get_settings().whisper.model == "medium"
    assert config.get_settings().logging.retention_days == 7


def test_update_settings_rejects_invalid_values(client):
    data = client.get("/api/settings").json()
    data["logging"]["retention_days"] = -5
    response = client.put("/api/settings", json=data)
    assert response.status_code == 422


def test_api_key_is_masked_and_preserved(client):
    data = client.get("/api/settings").json()
    data["llm"]["api_key"] = "geheim-123"
    response = client.put("/api/settings", json=data)
    assert response.json()["llm"]["api_key"] == API_KEY_MASK

    # sending the mask back keeps the stored key
    data = client.get("/api/settings").json()
    assert data["llm"]["api_key"] == API_KEY_MASK
    client.put("/api/settings", json=data)
    assert config.get_settings().llm.api_key == "geheim-123"


def test_setup_state_cannot_be_changed_via_settings_api(client):
    data = client.get("/api/settings").json()
    data["setup"]["completed"] = True
    client.put("/api/settings", json=data)
    assert config.get_settings().setup.completed is False
