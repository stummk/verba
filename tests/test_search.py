"""Semantic search: chunk index (FTS5 + sqlite-vec), hybrid query, consistency."""

from __future__ import annotations

import math
import re
import threading

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import pipeline, rag, transcripts, vectorstore, workspace

NO_CANCEL = threading.Event()


def fake_encode(texts, kind="passage"):
    """Deterministic bag-of-words vectors — similar texts get similar vectors."""
    out = []
    for text in texts:
        vector = [0.0] * 8
        for word in re.findall(r"\w+", text.lower()):
            vector[hash(word) % 8] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        out.append([x / norm for x in vector])
    return out


@pytest.fixture(autouse=True)
def search_env(tmp_path, monkeypatch):
    settings = config.get_settings()
    settings.general.workspaces_dir = str(tmp_path / "workspaces")
    config.save_settings(settings)
    db.init_db()
    monkeypatch.setattr(vectorstore, "_encode", fake_encode)
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    monkeypatch.setitem(job_queue._handlers, "index_file", lambda job, cancel, report: None)
    monkeypatch.setitem(job_queue._handlers, "reindex_search", lambda job, cancel, report: None)


def make_done_file(tmp_path, name="a.mp3", segments=(("", "Hallo Welt.", 0.0, 2.0),)):
    source = tmp_path / name
    source.write_bytes(b"x")
    project = workspace.create_project(f"P-{name}")
    [file_row] = workspace.import_paths(project, [str(source)])
    workspace.set_file_status(file_row["id"], "done")
    with db.get_conn() as conn:
        for idx, (speaker, text, start, end) in enumerate(segments):
            conn.execute(
                "INSERT INTO segments (file_id, idx, start_s, end_s, text, speaker) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (file_row["id"], idx, start, end, text, speaker),
            )
    return workspace.get_file(file_row["id"]), project


def test_index_file_writes_chunks_fts_and_vectors(tmp_path):
    file_row, _ = make_done_file(
        tmp_path, segments=(("Anna", "Die Katze schläft.", 0, 3), ("", "Der Hund bellt.", 3, 6))
    )
    count = vectorstore.index_file(file_row["id"])
    assert count == 1  # small file → one chunk

    status = vectorstore.status()
    assert status["files_indexed"] == 1
    assert status["chunk_count"] == 1
    assert status["index_models"] == [config.get_settings().search.embedding_model]
    assert status["last_index"]

    with vectorstore._vec_conn() as conn:
        chunk = conn.execute("SELECT * FROM chunks").fetchone()
        assert chunk["start_s"] == 0 and chunk["end_s"] == 6
        assert chunk["speakers"] == "Anna"
        fts = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"Katze\"'"
        ).fetchall()
        assert len(fts) == 1
        vectors = conn.execute("SELECT COUNT(*) AS n FROM vec_chunks").fetchone()
        assert vectors["n"] == 1


def test_chunks_keep_timestamps_per_chunk(tmp_path):
    long_text = "Wort " * 120  # ~600 chars per segment → several chunks
    file_row, _ = make_done_file(
        tmp_path,
        segments=tuple(("", long_text.strip(), i * 10.0, (i + 1) * 10.0) for i in range(4)),
    )
    vectorstore.index_file(file_row["id"])
    with db.get_conn() as conn:
        rows = conn.execute("SELECT start_s, end_s FROM chunks ORDER BY chunk_index").fetchall()
    assert len(rows) > 1
    assert rows[0]["start_s"] == 0.0
    assert rows[-1]["end_s"] == 40.0
    assert all(row["end_s"] > row["start_s"] for row in rows)


def test_hybrid_search_finds_semantic_and_exact_matches(tmp_path):
    cat_file, _ = make_done_file(
        tmp_path, "cat.mp3", segments=(("", "Die Katze schläft auf dem Sofa.", 0, 5),)
    )
    zebra_file, _ = make_done_file(
        tmp_path, "zebra.mp3", segments=(("", "Ein Zebra läuft durch die Steppe.", 0, 5),)
    )
    vectorstore.index_file(cat_file["id"])
    vectorstore.index_file(zebra_file["id"])

    results = vectorstore.search("Katze schläft")
    assert results and results[0]["file_id"] == cat_file["id"]
    assert results[0]["start_s"] == 0

    exact = vectorstore.search("Zebra")  # rare word → FTS half must catch it
    assert any(r["file_id"] == zebra_file["id"] for r in exact)


def test_search_filters(tmp_path):
    file_a, project_a = make_done_file(
        tmp_path, "a.mp3", segments=(("Anna", "Bericht über das Budget.", 0, 4),)
    )
    file_b, _ = make_done_file(
        tmp_path, "b.mp3", segments=(("Ben", "Bericht über das Budget.", 0, 4),)
    )
    with db.get_conn() as conn:
        conn.execute("UPDATE files SET recorded_at = '2024-01-01' WHERE id = ?", (file_a["id"],))
        conn.execute("UPDATE files SET recorded_at = '2025-06-01' WHERE id = ?", (file_b["id"],))
    vectorstore.index_file(file_a["id"])
    vectorstore.index_file(file_b["id"])

    by_project = vectorstore.search("Budget", {"project_id": project_a["id"]})
    assert {r["file_id"] for r in by_project} == {file_a["id"]}

    by_speaker = vectorstore.search("Budget", {"speaker": "Ben"})
    assert {r["file_id"] for r in by_speaker} == {file_b["id"]}

    by_date = vectorstore.search("Budget", {"date_from": "2025-01-01"})
    assert {r["file_id"] for r in by_date} == {file_b["id"]}


# ── header search & grouping ──────────────────────────────────────────


def set_header(file_id, **fields):
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE files SET {assignments} WHERE id = ?", (*fields.values(), file_id))


def test_header_search_finds_name_note_and_date(tmp_path):
    """A name, an extra note and a date live in the header, not in the text."""
    file_row, _ = make_done_file(tmp_path, "rede.mp3", segments=(("", "Guten Abend.", 0, 3),))
    set_header(
        file_row["id"],
        header_left="Max Mustermann",
        header_right="Zusatzhinweis: Entwurf",
        recorded_at="2024-05-12",
    )
    vectorstore.index_file(file_row["id"])

    for query in ("Mustermann", "Zusatzhinweis", "12.05.2024"):
        hits = vectorstore.search(query)
        header_hits = [hit for hit in hits if hit["source"] == "header"]
        assert header_hits, f"{query}: Kopfzeile wird nicht gefunden"
        assert header_hits[0]["file_id"] == file_row["id"]
        assert header_hits[0]["start_s"] == 0.0
        assert "Mustermann" in header_hits[0]["text"]


def test_header_search_ignores_case_beyond_ascii(tmp_path):
    """German and Russian names in caps have to match too."""
    file_row, _ = make_done_file(tmp_path, "u.mp3", segments=(("", "Inhalt.", 0, 3),))
    set_header(file_row["id"], header_left="MÜLLER", header_right="МОСКВА")
    vectorstore.index_file(file_row["id"])

    for query in ("müller", "москва"):
        assert any(h["source"] == "header" for h in vectorstore.search(query)), query


def test_header_search_needs_every_token(tmp_path):
    """AND semantics: a whole question must not drag files in by one word."""
    file_row, _ = make_done_file(tmp_path, "h.mp3", segments=(("", "Inhalt.", 0, 3),))
    set_header(file_row["id"], header_left="Anna Berg", title="Interview")
    vectorstore.index_file(file_row["id"])

    assert any(h["source"] == "header" for h in vectorstore.search("Anna Berg"))
    assert not any(h["source"] == "header" for h in vectorstore.search("Anna Berg über das Budget"))


def test_header_search_respects_the_filters(tmp_path):
    file_a, project_a = make_done_file(tmp_path, "a.mp3", segments=(("", "Eins.", 0, 3),))
    file_b, _ = make_done_file(tmp_path, "b.mp3", segments=(("", "Zwei.", 0, 3),))
    for file_row in (file_a, file_b):
        set_header(file_row["id"], header_left="Mustermann")
        vectorstore.index_file(file_row["id"])

    hits = vectorstore.search("Mustermann", {"project_id": project_a["id"]})
    assert {h["file_id"] for h in hits if h["source"] == "header"} == {file_a["id"]}


def test_transcript_hits_are_marked_as_such(tmp_path):
    file_row, _ = make_done_file(tmp_path, segments=(("", "Ein seltenes Nashorn.", 0, 3),))
    vectorstore.index_file(file_row["id"])
    hits = vectorstore.search("Nashorn")
    assert hits and all(hit["source"] == "transcript" for hit in hits)


def test_group_by_file_lists_every_file_once(tmp_path):
    long_text = "Wort " * 120  # several chunks, so one file can match twice
    file_row, _ = make_done_file(
        tmp_path,
        "lang.mp3",
        segments=tuple(("", long_text.strip(), i * 10.0, (i + 1) * 10.0) for i in range(4)),
    )
    set_header(file_row["id"], header_left="Mustermann", title="Langes Gespräch")
    vectorstore.index_file(file_row["id"])

    # the vector half ranks every chunk of the file, the header matches too
    groups = vectorstore.group_by_file(vectorstore.search("Mustermann"))

    assert [group["file_id"] for group in groups] == [file_row["id"]]  # exactly once
    group = groups[0]
    assert group["title"] == "Langes Gespräch"
    assert group["header"] == "Mustermann"
    assert len(group["hits"]) > 1
    # the header leads, the passages follow in timeline order
    assert group["hits"][0]["source"] == "header"
    starts = [hit["start_s"] for hit in group["hits"]]
    assert starts == sorted(starts)


def test_delete_file_removes_index_entries_immediately(tmp_path):
    file_row, _ = make_done_file(tmp_path, segments=(("", "Einzigartiger Flamingo.", 0, 3),))
    vectorstore.index_file(file_row["id"])
    workspace.delete_file(file_row["id"])

    assert vectorstore.status()["chunk_count"] == 0
    with vectorstore._vec_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM vec_chunks").fetchone()["n"] == 0
        ghosts = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"Flamingo\"'"
        ).fetchall()
        assert ghosts == []


def test_delete_project_removes_index_entries(tmp_path):
    file_row, project = make_done_file(tmp_path, segments=(("", "Projektinhalt.", 0, 3),))
    vectorstore.index_file(file_row["id"])
    workspace.delete_project(project["id"])
    assert vectorstore.status()["chunk_count"] == 0


def test_maybe_enqueue_index_dedupes_and_requires_done(tmp_path):
    file_row, _ = make_done_file(tmp_path)
    job = vectorstore.maybe_enqueue_index(file_row["id"], session_id="s1")
    assert job is not None and job["kind"] == "index_file"
    assert vectorstore.maybe_enqueue_index(file_row["id"]) is None  # deduped

    workspace.set_file_status(file_row["id"], "pending")
    with db.get_conn() as conn:
        conn.execute("UPDATE jobs SET status = 'done'")
    assert vectorstore.maybe_enqueue_index(file_row["id"]) is None  # not done


def test_reindex_rebuilds_everything(tmp_path):
    file_row, _ = make_done_file(tmp_path, segments=(("", "Alter Inhalt.", 0, 3),))
    vectorstore.index_file(file_row["id"])

    reports = []
    vectorstore.handle_reindex_job({"payload": {}}, NO_CANCEL, lambda p, m: reports.append((p, m)))
    status = vectorstore.status()
    assert status["files_indexed"] == 1 and status["chunk_count"] == 1
    assert reports[-1][0] == 100


def test_rag_ask_answers_from_sources(monkeypatch, tmp_path):
    file_row, _ = make_done_file(tmp_path, segments=(("", "Der Termin ist am Freitag.", 0, 3),))
    vectorstore.index_file(file_row["id"])

    seen = {}

    def fake_chat(messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        return "Der Termin ist am Freitag [1]."

    monkeypatch.setattr("verba.services.llm.chat", fake_chat)
    result = rag.ask("Wann ist der Termin?")
    assert result["answer"].endswith("[1].")
    assert len(result["sources"]) == 1
    assert "[1]" in seen["user"] and "Freitag" in seen["user"]


def test_rag_marks_a_header_source_as_metadata(monkeypatch, tmp_path):
    """The model must not quote a name from the header as spoken text."""
    file_row, _ = make_done_file(tmp_path, segments=(("", "Guten Tag.", 0, 3),))
    set_header(file_row["id"], header_left="Mustermann")
    vectorstore.index_file(file_row["id"])

    seen = {}

    def fake_chat(messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        return "Antwort [1]."

    monkeypatch.setattr("verba.services.llm.chat", fake_chat)
    rag.ask("Mustermann")
    assert "Kopfdaten" in seen["user"]


def test_rag_ask_without_hits_never_calls_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("LLM darf ohne Treffer nicht aufgerufen werden")

    monkeypatch.setattr("verba.services.llm.chat", boom)
    result = rag.ask("völlig unbekanntes Thema")
    assert result == {"answer": "", "sources": []}


def test_derived_text_edit_keeps_index_consistent(tmp_path):
    """Segment edits via the API enqueue a re-index for just that file."""
    file_row, _ = make_done_file(tmp_path, segments=(("", "Alter Text.", 0, 3),))
    vectorstore.index_file(file_row["id"])
    with db.get_conn() as conn:
        segment_id = conn.execute("SELECT id FROM segments").fetchone()["id"]
    transcripts.update_segment(segment_id, {"text": "Neuer Text."})
    job = vectorstore.maybe_enqueue_index(file_row["id"])
    assert job is not None and job["file_id"] == file_row["id"]


# ── API layer ─────────────────────────────────────────────────────────


def test_search_endpoint_409_when_components_missing(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: False)
    response = client.post("/api/search", json={"query": "x"})
    assert response.status_code == 409


def test_search_status_endpoint(client):
    data = client.get("/api/search/status").json()
    assert {"available", "files_indexed", "chunk_count", "configured_model"} <= set(data)


def test_ask_endpoint_requires_llm(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    response = client.post("/api/search/ask", json={"query": "x"})
    assert response.status_code == 409
    assert "LLM" in response.json()["detail"]


def test_model_change_triggers_reindex(client, monkeypatch):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    settings = client.get("/api/settings").json()
    settings["search"]["embedding_model"] = "intfloat/multilingual-e5-small"
    assert client.put("/api/settings", json=settings).status_code == 200
    jobs = client.get("/api/jobs").json()
    assert any(j["kind"] == "reindex_search" for j in jobs)


def test_search_endpoint_returns_files_with_their_hits(client, monkeypatch, tmp_path):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    file_row, _ = make_done_file(tmp_path, segments=(("", "Der Termin ist am Freitag.", 0, 3),))
    vectorstore.index_file(file_row["id"])

    data = client.post("/api/search", json={"query": "Termin"}).json()

    assert len(data["results"]) == 1
    group = data["results"][0]
    assert group["file_id"] == file_row["id"]
    assert group["hits"] and group["hits"][0]["source"] == "transcript"
    assert "Freitag" in group["hits"][0]["text"]


def test_status_endpoint_says_whether_an_llm_is_available(client):
    """The AI-answer button sits next to the search button — before any search."""
    assert client.get("/api/search/status").json()["llm_available"] is False


def test_cleanup_pipeline_available_flag_in_search_response(client, monkeypatch, tmp_path):
    monkeypatch.setattr(vectorstore, "available", lambda: True)
    monkeypatch.setattr(vectorstore, "search", lambda *a, **k: [])
    data = client.post("/api/search", json={"query": "irgendwas"}).json()
    assert data["results"] == []
    assert data["llm_available"] is False


def test_pipeline_save_text_does_not_touch_chunks(tmp_path):
    """Derived texts and the search index are independent stores."""
    file_row, _ = make_done_file(tmp_path)
    vectorstore.index_file(file_row["id"])
    pipeline.save_text(file_row["id"], "cleanup", "Bereinigt.")
    assert vectorstore.status()["chunk_count"] == 1
