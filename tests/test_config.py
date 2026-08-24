from __future__ import annotations

import json

from verba import config


def test_defaults_when_no_file_exists():
    settings = config.get_settings()
    assert settings.server.port == 8710
    assert settings.llm.mode == "none"
    assert settings.setup.completed is False


def test_save_and_reload_roundtrip():
    settings = config.get_settings()
    settings.whisper.model = "large-v3"
    settings.logging.retention_days = 30
    config.save_settings(settings)

    config.reset_cache()
    reloaded = config.get_settings()
    assert reloaded.whisper.model == "large-v3"
    assert reloaded.logging.retention_days == 30


def test_corrupt_settings_file_falls_back_to_defaults():
    config.settings_path().write_text("{ kaputt", encoding="utf-8")
    config.reset_cache()
    settings = config.get_settings()
    assert settings.server.port == 8710


def test_invalid_values_fall_back_to_defaults():
    config.settings_path().write_text(json.dumps({"server": {"port": 999999}}), encoding="utf-8")
    config.reset_cache()
    settings = config.get_settings()
    assert settings.server.port == 8710


def test_legacy_env_var_still_resolves_data_dir(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy-data"
    monkeypatch.delenv("VERBA_DATA_DIR", raising=False)
    monkeypatch.setenv("TRANSKRIPTOR_DATA_DIR", str(legacy))
    assert config.data_dir() == legacy


def test_new_env_var_wins_over_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "new-data"))
    monkeypatch.setenv("TRANSKRIPTOR_DATA_DIR", str(tmp_path / "old-data"))
    assert config.data_dir() == tmp_path / "new-data"


def test_directories_are_created():
    settings = config.get_settings()
    assert config.models_dir(settings).is_dir()
    assert config.logs_dir().is_dir()
    assert config.tools_dir().is_dir()
