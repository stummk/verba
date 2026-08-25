from __future__ import annotations

import sys
from pathlib import Path

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


# ── pointing the settings at an existing model collection ─────────────


def test_changed_models_dir_is_picked_up_without_a_restart(tmp_path):
    """A folder full of models becomes visible as soon as the path is saved:
    the list is read from disk on every request, never cached."""
    existing = tmp_path / "M" / "Modelle" / "whisper"
    for rel in ("large-v3", "faster-whisper-large-v3", "eigene/mein-finetune"):
        (existing / rel).mkdir(parents=True)
        (existing / rel / "model.bin").write_bytes(b"")
    cache = existing / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    cache.mkdir(parents=True)
    (cache / "model.bin").write_bytes(b"")

    settings = config.get_settings()
    settings.whisper.models_dir = str(existing)
    config.save_settings(settings)

    result = whisper.list_models()
    local = {name.replace("\\", "/") for name in result["local"]}
    assert local == {"large-v3", "faster-whisper-large-v3", "eigene/mein-finetune"}
    # a folder named like a builtin, and a complete HF cache, count as installed
    assert set(result["installed"]) == {"small", "large-v3"}


def test_a_folder_without_a_model_is_ignored(tmp_path):
    existing = tmp_path / "collection"
    (existing / "readme").mkdir(parents=True)
    (existing / "readme" / "notes.txt").write_bytes(b"nothing")
    incomplete = existing / "models--Systran--faster-whisper-medium" / "snapshots" / "def"
    incomplete.mkdir(parents=True)
    (incomplete / "config.json").write_bytes(b"{}")  # aborted download

    settings = config.get_settings()
    settings.whisper.models_dir = str(existing)
    config.save_settings(settings)

    assert whisper.list_models()["local"] == []
    assert "medium" not in whisper.list_models()["installed"]


def test_a_missing_models_dir_is_created(tmp_path):
    target = tmp_path / "not-yet-there" / "whisper"
    settings = config.get_settings()
    settings.whisper.models_dir = str(target)
    config.save_settings(settings)

    assert whisper.list_models()["local"] == []
    assert target.is_dir()


def test_the_models_dir_is_part_of_the_model_cache_key(tmp_path, monkeypatch):
    """Changing the directory must reload the model, not keep serving the one
    loaded from the old path."""
    loaded = []

    class FakeWhisperModel:
        def __init__(self, model_ref, **kwargs):
            loaded.append(kwargs["download_root"])

    module = type(sys)("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    whisper.unload_model()

    settings = config.get_settings()
    for name in ("first", "second"):
        settings.whisper.models_dir = str(tmp_path / name)
        config.save_settings(settings)
        whisper.get_model()

    assert loaded == [str(tmp_path / "first"), str(tmp_path / "second")]
    whisper.unload_model()


# ── models are loaded from where they lie, never copied ───────────────


def _capture_model_loads(monkeypatch) -> list[dict]:
    """Replace faster_whisper.WhisperModel with a recorder."""
    calls: list[dict] = []

    class FakeWhisperModel:
        def __init__(self, model_ref, **kwargs):
            calls.append({"ref": model_ref, **kwargs})

    module = type(sys)("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    whisper.unload_model()
    return calls


def _tree(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def test_a_local_model_is_loaded_in_place(tmp_path, monkeypatch):
    """The absolute path of the folder goes to faster-whisper — no copy into
    the data directory, no download."""
    collection = tmp_path / "M" / "Modelle" / "whisper"
    model = collection / "large-v3"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"weights")
    before = _tree(collection)

    settings = config.get_settings()
    settings.whisper.models_dir = str(collection)
    settings.whisper.model = "large-v3"
    settings.whisper.device = "cpu"
    config.save_settings(settings)

    calls = _capture_model_loads(monkeypatch)
    whisper.get_model()

    assert calls[0]["ref"] == str(model)  # the folder itself, not the name
    assert calls[0]["download_root"] == str(collection)
    assert _tree(collection) == before  # nothing added, nothing moved
    whisper.unload_model()


def test_a_nested_local_model_is_loaded_in_place(tmp_path, monkeypatch):
    collection = tmp_path / "collection"
    model = collection / "eigene" / "mein-finetune"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"weights")

    settings = config.get_settings()
    settings.whisper.models_dir = str(collection)
    settings.whisper.model = whisper.list_local_models()[0]
    settings.whisper.device = "cpu"
    config.save_settings(settings)

    calls = _capture_model_loads(monkeypatch)
    whisper.get_model()

    assert Path(calls[0]["ref"]) == model
    whisper.unload_model()


def test_a_builtin_in_the_hf_cache_stays_in_the_configured_dir(tmp_path, monkeypatch):
    """Cache layout: the name is passed through, but download_root points at
    the configured directory — so the cache there is used as it is."""
    collection = tmp_path / "collection"
    cache = collection / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    cache.mkdir(parents=True)
    (cache / "model.bin").write_bytes(b"weights")
    before = _tree(collection)

    settings = config.get_settings()
    settings.whisper.models_dir = str(collection)
    settings.whisper.model = "small"
    settings.whisper.device = "cpu"
    config.save_settings(settings)

    calls = _capture_model_loads(monkeypatch)
    whisper.get_model()

    assert calls[0]["ref"] == "small"
    assert calls[0]["download_root"] == str(collection)
    assert _tree(collection) == before
    whisper.unload_model()


def test_the_data_dir_stays_untouched_when_a_collection_is_configured(tmp_path, monkeypatch):
    """Regression guard: nothing must land in <data>/models any more."""
    collection = tmp_path / "collection"
    model = collection / "large-v3"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"weights")

    default_models = config.data_dir() / "models"
    default_models.mkdir(parents=True, exist_ok=True)
    before = _tree(default_models)

    settings = config.get_settings()
    settings.whisper.models_dir = str(collection)
    settings.whisper.model = "large-v3"
    config.save_settings(settings)

    _capture_model_loads(monkeypatch)
    whisper.get_model()

    assert _tree(default_models) == before
    whisper.unload_model()
