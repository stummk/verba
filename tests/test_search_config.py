"""The embedding model is a choice from a catalog, not a free-text field.

A mistyped model id would only fail deep inside the first index run, and a
monolingual model would silently rank German queries against English text —
so the catalog is the contract, and the settings fall back to the default
instead of refusing to start.
"""

from __future__ import annotations

import pytest

from verba import config, db
from verba.services import vectorstore


@pytest.fixture(autouse=True)
def clean_settings():
    config.save_settings(config.Settings())
    db.init_db()
    vectorstore.unload_model()


# ── the catalog ───────────────────────────────────────────────────────


def test_every_catalog_entry_is_multilingual_and_cpu_sized():
    assert len(config.EMBEDDING_MODELS) >= 1
    for entry in config.EMBEDDING_MODELS:
        assert entry.languages >= 15, entry.name
        # download in minutes, not hours — the large tier (BGE-M3) is the
        # deliberate upper end of what a CPU-only machine should pull
        assert entry.size_mb <= 2500, entry.name
        assert entry.dim > 0
        assert entry.speed in ("fast", "balanced", "quality")


def test_a_large_high_quality_option_exists_next_to_the_small_default():
    """Small and fast by default, but BGE-M3 for whoever wants the quality."""
    default = config.embedding_model(config.DEFAULT_EMBEDDING_MODEL)
    assert default.size_mb <= 600 and default.speed == "fast"
    large = config.embedding_model("BAAI/bge-m3")
    assert large.name == "BAAI/bge-m3", "BGE-M3 is expected in the catalog"
    assert large.languages >= 100
    assert large.dim == 1024
    # the English BGE models expect an instruction prefix, BGE-M3 does not
    assert large.query_prefix == "" and large.passage_prefix == ""


def test_the_default_is_the_first_entry_and_fast():
    assert config.EMBEDDING_MODELS[0].name == config.DEFAULT_EMBEDDING_MODEL
    assert config.embedding_model(config.DEFAULT_EMBEDDING_MODEL).speed == "fast"
    assert config.Settings().search.embedding_model == config.DEFAULT_EMBEDDING_MODEL


def test_an_unknown_model_falls_back_to_the_default():
    settings = config.Settings.model_validate({"search": {"embedding_model": "typo/mdel"}})
    assert settings.search.embedding_model == config.DEFAULT_EMBEDDING_MODEL


def test_a_catalog_model_is_kept():
    name = config.EMBEDDING_MODELS[-1].name
    settings = config.Settings.model_validate({"search": {"embedding_model": name}})
    assert settings.search.embedding_model == name


def test_lookup_of_an_unknown_name_returns_the_default_entry():
    assert config.embedding_model("nope").name == config.DEFAULT_EMBEDDING_MODEL
    assert config.embedding_model().name == config.DEFAULT_EMBEDDING_MODEL


# ── query and passage prefixes ────────────────────────────────────────


def encode_spy(monkeypatch):
    """Capture the strings actually handed to the model."""
    seen: list[list[str]] = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            seen.append(list(texts))
            return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(vectorstore, "_load_model", lambda: FakeModel())
    return seen


def test_a_model_without_prefixes_gets_the_plain_text(monkeypatch):
    seen = encode_spy(monkeypatch)
    vectorstore._encode(["Hallo Welt"], "query")
    assert seen == [["Hallo Welt"]]


def test_the_e5_family_gets_its_query_and_passage_prefixes(monkeypatch):
    """E5 is trained with them and loses noticeable quality without."""
    e5 = next(entry for entry in config.EMBEDDING_MODELS if entry.query_prefix)
    settings = config.get_settings()
    settings.search.embedding_model = e5.name
    config.save_settings(settings)
    seen = encode_spy(monkeypatch)

    vectorstore._encode(["Frage"], "query")
    vectorstore._encode(["Abschnitt"], "passage")

    assert seen == [[f"{e5.query_prefix}Frage"], [f"{e5.passage_prefix}Abschnitt"]]


def test_a_failed_model_load_says_what_to_do(monkeypatch):
    """The one moment this fails is the download on first use."""
    import sys
    import types

    module = types.ModuleType("sentence_transformers")

    def boom(*args, **kwargs):
        raise OSError("no network")

    module.SentenceTransformer = boom
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    vectorstore.unload_model()

    with pytest.raises(vectorstore.EmbeddingUnavailable, match="heruntergeladen"):
        vectorstore._load_model()


# ── API ───────────────────────────────────────────────────────────────


def seed_chunk(model: str) -> None:
    """One indexed chunk, built with `model` — enough for the status report."""
    with db.get_conn() as conn:
        project = conn.execute(
            "INSERT INTO projects (name, slug, workspace) VALUES ('P', 'p', 'nirgendwo')"
        )
        file_row = conn.execute(
            "INSERT INTO files (project_id, filename, rel_path, status) "
            "VALUES (?, 'a.mp3', 'audio/a.mp3', 'done')",
            (project.lastrowid,),
        )
        conn.execute(
            "INSERT INTO chunks (file_id, chunk_index, start_s, end_s, text, speakers, model) "
            "VALUES (?, 0, 0, 1, 'text', '', ?)",
            (file_row.lastrowid, model),
        )


def test_the_catalog_endpoint_lists_the_choices(client):
    data = client.get("/api/search/models").json()
    assert [model["name"] for model in data["models"]] == [
        entry.name for entry in config.EMBEDDING_MODELS
    ]
    assert data["default"] == config.DEFAULT_EMBEDDING_MODEL
    assert data["configured"] == config.DEFAULT_EMBEDDING_MODEL
    assert data["cache_dir"].endswith("embeddings")
    assert {"label", "dim", "size_mb", "languages", "speed"} <= set(data["models"][0])


def test_the_status_reports_the_label_and_a_model_mismatch(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    seed_chunk("some/older-model")

    status = client.get("/api/search/status").json()

    assert status["configured_label"] == config.EMBEDDING_MODELS[0].label
    assert status["model_mismatch"] is True


def test_no_mismatch_when_the_index_matches(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    seed_chunk(config.DEFAULT_EMBEDDING_MODEL)
    assert client.get("/api/search/status").json()["model_mismatch"] is False


def test_a_search_without_a_usable_model_reports_service_unavailable(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)

    def boom(*args, **kwargs):
        raise vectorstore.EmbeddingUnavailable("Modell fehlt")

    monkeypatch.setattr(vectorstore, "search", boom)
    response = client.post("/api/search", json={"query": "test"})
    assert response.status_code == 503
    assert "Modell fehlt" in response.json()["detail"]


def test_switching_the_model_starts_a_reindex(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    other = config.EMBEDDING_MODELS[-1].name
    body = client.get("/api/settings").json()
    body["search"]["embedding_model"] = other

    response = client.put("/api/settings", json=body)

    assert response.json()["reindex_started"] is True
    assert any(job["kind"] == "reindex_search" for job in client.get("/api/jobs").json())


def test_saving_the_same_model_starts_nothing(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    body = client.get("/api/settings").json()
    response = client.put("/api/settings", json=body)
    assert response.json()["reindex_started"] is False
    assert not client.get("/api/jobs").json()


def test_reindex_stays_available_by_hand(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    assert client.post("/api/search/reindex").status_code == 200


# ── reusing a model that is already on disk ───────────────────────────


def place_model(root, folder, files=("config.json",)):
    """Fake a downloaded model folder."""
    target = root / folder
    target.mkdir(parents=True, exist_ok=True)
    for name in files:
        (target / name).write_text("{}", encoding="utf-8")
    return target


def configure_dir(path):
    settings = config.get_settings()
    settings.search.embeddings_dir = str(path)
    config.save_settings(settings)
    vectorstore.unload_model()


def test_a_configured_directory_is_used_as_is(tmp_path):
    collection = tmp_path / "F" / "Models" / "embeddings"
    configure_dir(collection)
    assert config.embeddings_dir() == collection


def test_an_empty_directory_keeps_the_default():
    assert config.embeddings_dir() == config.default_embeddings_dir()


def test_a_plain_folder_counts_as_present(tmp_path):
    configure_dir(tmp_path)
    entry = config.embedding_model("BAAI/bge-m3")
    assert vectorstore.local_model_dir(entry) is None

    folder = place_model(tmp_path, "bge-m3")
    assert vectorstore.local_model_dir(entry) == folder
    assert vectorstore.model_present_locally(entry) is True


def test_an_underscored_folder_name_also_counts(tmp_path):
    configure_dir(tmp_path)
    entry = config.embedding_model("BAAI/bge-m3")
    folder = place_model(tmp_path, "BAAI_bge-m3")
    assert vectorstore.local_model_dir(entry) == folder


def test_the_huggingface_cache_layout_counts(tmp_path):
    """A moved HF cache is the common case — that layout must be recognised."""
    configure_dir(tmp_path)
    entry = config.embedding_model("BAAI/bge-m3")
    snapshot = place_model(tmp_path, "models--BAAI--bge-m3/snapshots/abc123")
    assert vectorstore.local_model_dir(entry) == snapshot


def test_a_present_model_is_loaded_from_disk_not_downloaded(tmp_path, monkeypatch):
    configure_dir(tmp_path)
    folder = place_model(tmp_path, "paraphrase-multilingual-MiniLM-L12-v2")
    calls: list[dict] = []

    import sys
    import types

    module = types.ModuleType("sentence_transformers")

    class FakeSentenceTransformer:
        def __init__(self, ref, **kwargs):
            calls.append({"ref": ref, **kwargs})

    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    vectorstore._load_model()

    assert calls[0]["ref"] == str(folder)  # the folder itself, not the repo id
    assert calls[0]["cache_folder"] == str(tmp_path)


def test_a_missing_model_is_requested_by_name(tmp_path, monkeypatch):
    configure_dir(tmp_path)
    calls: list[dict] = []

    import sys
    import types

    module = types.ModuleType("sentence_transformers")

    class FakeSentenceTransformer:
        def __init__(self, ref, **kwargs):
            calls.append({"ref": ref, **kwargs})

    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    vectorstore._load_model()

    assert calls[0]["ref"] == config.DEFAULT_EMBEDDING_MODEL
    assert calls[0]["cache_folder"] == str(tmp_path)  # downloaded to the configured dir


def test_changing_the_directory_reloads_the_model(tmp_path, monkeypatch):
    """Otherwise the model from the old path would keep serving queries."""
    calls: list[str] = []

    import sys
    import types

    module = types.ModuleType("sentence_transformers")

    class FakeSentenceTransformer:
        def __init__(self, ref, **kwargs):
            calls.append(kwargs["cache_folder"])

    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    configure_dir(tmp_path / "erst")
    vectorstore._load_model()
    settings = config.get_settings()
    settings.search.embeddings_dir = str(tmp_path / "dann")  # no unload_model()
    config.save_settings(settings)
    vectorstore._load_model()

    assert calls == [str(tmp_path / "erst"), str(tmp_path / "dann")]


def test_the_catalog_endpoint_says_what_is_already_there(client, tmp_path):
    configure_dir(tmp_path)
    place_model(tmp_path, "bge-m3")

    models = {m["name"]: m["present"] for m in client.get("/api/search/models").json()["models"]}

    assert models["BAAI/bge-m3"] is True
    assert models[config.DEFAULT_EMBEDDING_MODEL] is False
