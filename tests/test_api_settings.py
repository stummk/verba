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


def test_the_update_check_can_be_switched_off_in_the_settings(client):
    """The only setting that reaches out to the internet stays the user's call."""
    data = client.get("/api/settings").json()
    assert data["updates"]["check_enabled"] is True

    data["updates"]["check_enabled"] = False
    assert client.put("/api/settings", json=data).status_code == 200

    config.reset_cache()
    assert config.get_settings().updates.check_enabled is False


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


def test_the_audio_backup_can_be_switched_off_in_the_settings(client):
    """Whether a cut recording leaves its original behind is the user's call."""
    data = client.get("/api/settings").json()
    assert data["general"]["audio_backup"] is True  # on unless it is turned off
    data["general"]["audio_backup"] = False

    assert client.put("/api/settings", json=data).status_code == 200

    config.reset_cache()
    assert config.get_settings().general.audio_backup is False


def test_the_thinking_budget_is_stored_and_bounded(client):
    data = client.get("/api/settings").json()
    assert data["llm"]["reasoning_budget"] == 8192  # a ceiling by default

    data["llm"]["reasoning_budget"] = 2048
    assert client.put("/api/settings", json=data).status_code == 200
    config.reset_cache()
    assert config.get_settings().llm.reasoning_budget == 2048

    # 0 is "no ceiling"; a negative number is not a budget at all
    data["llm"]["reasoning_budget"] = -1
    assert client.put("/api/settings", json=data).status_code == 422


def test_transcription_endpoint_key_is_masked_and_preserved(client):
    data = client.get("/api/settings").json()
    data["whisper"]["engine"] = "openai"
    data["whisper"]["base_url"] = "http://asr.test/v1"
    data["whisper"]["api_key"] = "asr-geheim"
    assert client.put("/api/settings", json=data).json()["whisper"]["api_key"] == API_KEY_MASK

    data = client.get("/api/settings").json()
    assert data["whisper"]["api_key"] == API_KEY_MASK
    assert client.put("/api/settings", json=data).status_code == 200

    config.reset_cache()
    assert config.get_settings().whisper.api_key == "asr-geheim"
    assert config.get_settings().whisper.engine == "openai"


def test_settings_without_an_engine_keep_faster_whisper(client):
    """An installation from before the engine choice transcribes as it did."""
    data = client.get("/api/settings").json()
    for key in ("engine", "base_url", "api_key", "remote_model"):
        data["whisper"].pop(key)
    assert client.put("/api/settings", json=data).status_code == 200

    config.reset_cache()
    assert config.get_settings().whisper.engine == "faster_whisper"


def test_transcription_endpoint_probe_uses_the_stored_key(client, monkeypatch):
    settings = config.get_settings()
    settings.whisper.api_key = "asr-geheim"
    config.save_settings(settings)
    seen = {}

    def fake_probe(base_url, api_key=""):
        seen.update(base_url=base_url, api_key=api_key)
        return {"ok": True, "models": ["whisper-large-v3-turbo"]}

    monkeypatch.setattr("verba.services.llm.probe", fake_probe)
    response = client.post(
        "/api/settings/whisper/test",
        json={"base_url": " http://asr.test/v1 ", "api_key": API_KEY_MASK},
    )

    assert response.json()["models"] == ["whisper-large-v3-turbo"]
    assert seen == {"base_url": "http://asr.test/v1", "api_key": "asr-geheim"}


def test_transcription_endpoint_probe_needs_a_url(client):
    response = client.post("/api/settings/whisper/test", json={"base_url": "  "})
    assert response.status_code == 422
