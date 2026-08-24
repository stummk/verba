from __future__ import annotations

import threading
from pathlib import Path

import pytest

from verba import config, db
from verba.services import pipeline, workspace


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    config.reset_cache()
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()


@pytest.fixture()
def file_with_segments(tmp_path):
    source = tmp_path / "speech.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("Pipeline")
    [file_row] = workspace.import_paths(project, [str(source)])
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO segments (file_id, idx, start_s, end_s, text) VALUES (?, ?, ?, ?, ?)",
            [
                (file_row["id"], 0, 0.0, 2.0, "ähm hallo welt"),
                (file_row["id"], 1, 2.0, 4.0, "zweiter satz"),
            ],
        )
    return file_row


def fake_chat(recorded: list):
    def chat(messages, model_override="", **kwargs):
        recorded.append(messages)
        user_text = messages[-1]["content"]
        if "übersetzt" in messages[0]["content"].lower():
            return f"[EN] {user_text}"
        return user_text.replace("ähm ", "")

    return chat


def run_job(file_id: int, payload_extra: dict) -> None:
    job = {"payload": {"file_id": file_id, **payload_extra}}
    pipeline.handle_llm_process_job(job, threading.Event(), lambda p, m="": None)


def test_cleanup_saves_derived_text_and_workspace_file(file_with_segments, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat(calls))

    run_job(file_with_segments["id"], {"steps": ["cleanup"]})

    texts = pipeline.list_texts(file_with_segments["id"])
    assert len(texts) == 1
    assert texts[0]["kind"] == "cleanup"
    assert "ähm" not in texts[0]["content"]
    assert "hallo welt" in texts[0]["content"]

    project = workspace.get_project(file_with_segments["project_id"])
    md = Path(project["workspace"]) / "transcripts" / "speech.cleanup.md"
    assert md.exists()
    assert "hallo welt" in md.read_text(encoding="utf-8")


def test_type_prompt_flows_into_system_message(file_with_segments, monkeypatch):
    from verba.services import project_types

    project_types.seed_builtin_types()
    lied = next(t for t in project_types.list_types() if t["key"] == "lied")
    workspace.update_project(file_with_segments["project_id"], {"type_id": lied["id"]})

    calls: list = []
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat(calls))
    run_job(file_with_segments["id"], {"steps": ["cleanup"]})

    system_message = calls[0][0]["content"]
    assert "Refrain" in system_message  # the Lied type prompt was appended


def test_translation_uses_cleanup_result(file_with_segments, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat(calls))

    run_job(
        file_with_segments["id"],
        {"steps": ["cleanup", "translate"], "target_language": "en"},
    )

    translation = pipeline.get_text(file_with_segments["id"], "translation", "en")
    assert translation is not None
    assert translation["content"].startswith("[EN]")
    assert "ähm" not in translation["content"]  # translated the cleaned text


def test_translation_without_cleanup_falls_back_to_segments(file_with_segments, monkeypatch):
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat([]))
    run_job(file_with_segments["id"], {"steps": ["translate"], "target_language": "ru"})

    translation = pipeline.get_text(file_with_segments["id"], "translation", "ru")
    assert translation is not None
    assert "zweiter satz" in translation["content"]


def test_cleanup_without_segments_fails(tmp_path, monkeypatch):
    source = tmp_path / "leer.mp3"
    source.write_bytes(b"x")
    project = workspace.create_project("Leer")
    [file_row] = workspace.import_paths(project, [str(source)])
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat([]))

    with pytest.raises(RuntimeError, match="No segments"):
        run_job(file_row["id"], {"steps": ["cleanup"]})


def test_rerun_overwrites_existing_text(file_with_segments, monkeypatch):
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat([]))
    run_job(file_with_segments["id"], {"steps": ["cleanup"]})
    run_job(file_with_segments["id"], {"steps": ["cleanup"]})
    texts = pipeline.list_texts(file_with_segments["id"])
    assert len(texts) == 1  # upsert, no duplicates


def test_any_language_is_a_valid_translation_target(file_with_segments, monkeypatch):
    """Whisper-supported codes resolve to English names for the prompt;
    unknown codes pass through unchanged — no allowlist anywhere."""
    from verba.services.languages import LANGUAGE_NAMES, language_name

    assert len(LANGUAGE_NAMES) >= 99
    assert language_name("fr") == "French"
    assert language_name("yue") == "Cantonese"
    assert language_name("xx") == "xx"

    seen = {}

    def capture(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        return "traduit"

    monkeypatch.setattr(pipeline.llm, "chat", capture)
    run_job(file_with_segments["id"], {"steps": ["translate"], "target_language": "fr"})
    assert "French" in seen["system"]
    translation = pipeline.get_text(file_with_segments["id"], "translation", "fr")
    assert translation["content"] == "traduit"


def test_frontend_and_backend_language_lists_stay_in_sync():
    """The language codes are intentionally maintained twice (the frontend has
    no build step, the backend must not import faster-whisper at core start);
    this guard catches one-sided additions or removals."""
    import re
    from pathlib import Path

    from verba.services.languages import LANGUAGE_NAMES

    frontend = Path(__file__).resolve().parents[1] / "frontend"
    source = (frontend / "js" / "languages.js").read_text(encoding="utf-8")
    array = re.search(r"WHISPER_LANGUAGES = \[(.*?)\];", source, re.DOTALL).group(1)
    frontend_codes = set(re.findall(r'"([a-z]+)"', array))
    assert frontend_codes == set(LANGUAGE_NAMES)
