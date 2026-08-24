from __future__ import annotations

import pytest

from verba import config
from verba.services import whisper


def test_builtin_models_listed(client):
    data = client.get("/api/models").json()
    assert "small" in data["builtin"]
    assert data["local"] == []


def test_local_models_found_in_subfolders():
    root = config.models_dir(config.get_settings())
    deep = root / "eigene" / "whisper-de"
    deep.mkdir(parents=True)
    (deep / "model.bin").write_bytes(b"")

    local = whisper.list_local_models()
    assert len(local) == 1
    assert local[0].replace("\\", "/") == "eigene/whisper-de"


def test_hf_cache_folders_are_hidden_from_local_list():
    root = config.models_dir(config.get_settings())
    cache = root / "models--Systran--faster-whisper-small" / "snapshots" / "abc123"
    cache.mkdir(parents=True)
    (cache / "model.bin").write_bytes(b"")

    assert whisper.list_local_models() == []
    assert "small" in whisper.installed_builtin_models()


def test_managed_download_counts_as_installed():
    root = config.models_dir(config.get_settings())
    (root / "tiny").mkdir(parents=True)
    (root / "tiny" / "model.bin").write_bytes(b"")

    assert whisper.list_local_models() == ["tiny"]
    assert "tiny" in whisper.installed_builtin_models()


def test_delete_model_removes_managed_folder():
    root = config.models_dir(config.get_settings())
    (root / "tiny").mkdir(parents=True)
    (root / "tiny" / "model.bin").write_bytes(b"")

    whisper.delete_model("tiny")
    assert not (root / "tiny").exists()


def test_delete_model_removes_hf_cache_of_builtin():
    root = config.models_dir(config.get_settings())
    cache = root / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    cache.mkdir(parents=True)
    (cache / "model.bin").write_bytes(b"")
    assert "small" in whisper.installed_builtin_models()

    whisper.delete_model("small")
    assert not (root / "models--Systran--faster-whisper-small").exists()
    assert "small" not in whisper.installed_builtin_models()


def test_delete_model_unknown_name_raises():
    with pytest.raises(FileNotFoundError):
        whisper.delete_model("nonexistent")


def test_delete_model_traversal_rejected():
    with pytest.raises(ValueError):
        whisper.delete_model("../../etc")


def test_local_folder_wins_over_hub_name():
    root = config.models_dir(config.get_settings())
    folder = root / "small"
    folder.mkdir(parents=True)
    (folder / "model.bin").write_bytes(b"")
    assert whisper._resolve_model_ref("small") == str(folder)
    assert whisper._resolve_model_ref("nonexistent") == "nonexistent"


def test_hf_cache_fallback_matches_faster_whisper_mapping():
    """The static fallback patterns must stay in sync with faster-whisper's
    own name→repo mapping (e.g. large-v3-turbo lives under mobiuslabsgmbh)."""
    fw_utils = pytest.importorskip("faster_whisper.utils")
    for name in whisper.BUILTIN_MODELS:
        expected = f"models--{fw_utils._MODELS[name].replace('/', '--')}"
        # mapping entry and fallback agree → exactly one deduped candidate
        assert whisper._hf_cache_dirs(name) == [expected]


def test_hf_cache_dirs_do_not_collide_across_builtins():
    """No builtin may claim another builtin's cache dir (large-v3 must never
    match distil-large-v3's folder)."""
    dirs = {name: set(whisper._hf_cache_dirs(name)) for name in whisper.BUILTIN_MODELS}
    for a, a_dirs in dirs.items():
        for b, b_dirs in dirs.items():
            if a != b:
                assert a_dirs.isdisjoint(b_dirs), f"{a} collides with {b}"


def test_partial_hf_cache_download_not_reported_installed():
    root = config.models_dir(config.get_settings())
    # crashed on-demand download: skeleton exists, no complete snapshot
    (root / "models--Systran--faster-whisper-small" / "blobs").mkdir(parents=True)

    assert "small" not in whisper.installed_builtin_models()


def test_delete_model_removes_orphaned_partial_folder():
    root = config.models_dir(config.get_settings())
    orphan = root / "small"
    orphan.mkdir(parents=True)
    (orphan / "model.bin.part").write_bytes(b"")  # no model.bin — partial download

    whisper.delete_model("small")
    assert not orphan.exists()


def test_delete_model_protects_folders_containing_nested_models():
    root = config.models_dir(config.get_settings())
    nested = root / "eigene" / "whisper-de"
    nested.mkdir(parents=True)
    (nested / "model.bin").write_bytes(b"")

    with pytest.raises(FileNotFoundError):
        whisper.delete_model("eigene")
    assert (nested / "model.bin").exists()


def test_delete_model_unloads_the_loaded_model(monkeypatch):
    root = config.models_dir(config.get_settings())
    (root / "tiny").mkdir(parents=True)
    (root / "tiny" / "model.bin").write_bytes(b"")
    monkeypatch.setattr(whisper, "_model", object())
    monkeypatch.setattr(whisper, "_model_key", ("tiny", "cpu", "int8"))

    whisper.delete_model("tiny")
    assert whisper._model is None
    assert whisper._model_key is None
