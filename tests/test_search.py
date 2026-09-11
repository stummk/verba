"""Semantic search: chunk index (FTS5 + sqlite-vec), hybrid query, consistency."""

from __future__ import annotations

import math
import re
import threading
import zlib

import pytest

from verba import config, db
from verba.core.jobs import job_queue
from verba.services import pipeline, rag, transcripts, vectorstore, workspace

NO_CANCEL = threading.Event()


# Wide enough that two unrelated sentences land near-orthogonal, the way a
# real embedding model has them: with a handful of buckets everything collides
# with everything, and a distance threshold could not be tested at all.
FAKE_DIM = 64


def fake_encode(texts, kind="passage"):
    """Deterministic bag-of-words vectors — similar texts get similar vectors."""
    out = []
    for text in texts:
        vector = [0.0] * FAKE_DIM
        for word in re.findall(r"\w+", text.lower()):
            vector[zlib.crc32(word.encode()) % FAKE_DIM] += 1.0
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


def test_a_file_row_reports_whether_it_is_indexed(tmp_path):
    """The card's search badge reads `index_chunks` — nothing else says it."""
    file_row, project = make_done_file(tmp_path, segments=(("", "Die Katze schläft.", 0, 3),))
    assert file_row["index_chunks"] == 0

    vectorstore.index_file(file_row["id"])
    assert workspace.get_file(file_row["id"])["index_chunks"] == 1
    assert workspace.list_files(project["id"])[0]["index_chunks"] == 1

    vectorstore.remove_file(file_row["id"])
    assert workspace.get_file(file_row["id"])["index_chunks"] == 0


def test_the_index_job_announces_the_file_it_indexed(monkeypatch, tmp_path):
    """No file field changes, so without this event the badge stays grey."""
    file_row, _ = make_done_file(tmp_path, segments=(("", "Der Hund bellt.", 0, 3),))
    events = []
    monkeypatch.setattr(
        workspace.hub, "publish", lambda kind, data=None, **kw: events.append((kind, data))
    )
    vectorstore.handle_index_file_job(
        {"payload": {"file_id": file_row["id"]}}, NO_CANCEL, lambda p, m: None
    )
    updates = [data for kind, data in events if kind == "file.update"]
    assert updates and updates[-1]["index_chunks"] == 1


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

    # the date is looked up in the form it is typed, which is the one the
    # import wrote into recorded_at — the header field carries the ISO one
    set_header(file_row["id"], recorded_at="12.05.2024")

    for query in ("Mustermann", "Zusatzhinweis", "12.05.2024"):
        hits = vectorstore.search(query)
        header_hits = [hit for hit in hits if hit["source"] == "header"]
        assert header_hits, f"{query}: Kopfzeile wird nicht gefunden"
        assert header_hits[0]["file_id"] == file_row["id"]
        assert header_hits[0]["start_s"] == 0.0


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
    text = "Mustermann berichtet. " * 30  # several chunks, all of them matching
    file_row, _ = make_done_file(
        tmp_path,
        "lang.mp3",
        segments=tuple(("", text.strip(), i * 10.0, (i + 1) * 10.0) for i in range(4)),
    )
    set_header(file_row["id"], header_left="Mustermann", title="Langes Gespräch")
    vectorstore.index_file(file_row["id"])

    groups = vectorstore.group_by_file(vectorstore.search("Mustermann"))

    assert [group["file_id"] for group in groups] == [file_row["id"]]  # exactly once
    group = groups[0]
    # the header the user wrote names the file, never the frozen import title
    assert group["label"] == "Mustermann"
    assert "title" not in group and "recorded_at" not in group
    assert len(group["hits"]) > 1
    # the header leads, the passages follow in timeline order
    assert group["hits"][0]["source"] == "header"
    starts = [hit["start_s"] for hit in group["hits"]]
    assert starts == sorted(starts)


def test_a_file_is_named_by_its_header_and_falls_back_to_the_file_name(tmp_path):
    """The import title is stale by design — it must never name a file."""
    file_row, _ = make_done_file(tmp_path, "ru_de_Wesner.mp3", segments=(("", "Text.", 0, 3),))
    set_header(file_row["id"], title="ru de Wesner Ronald", header_left="", header_right="")
    assert vectorstore.display_label(workspace.get_file(file_row["id"])) == "ru_de_Wesner.mp3"

    set_header(file_row["id"], header_left="Wesner Ronald", header_right="Bremen, 2026-07-31")
    label = vectorstore.display_label(workspace.get_file(file_row["id"]))
    assert label == "Wesner Ronald · Bremen, 2026-07-31"  # the empty middle is left out


def test_a_header_hit_quotes_only_the_fields_that_matched(tmp_path):
    file_row, _ = make_done_file(tmp_path, "protokoll.mp3", segments=(("", "Text.", 0, 3),))
    set_header(file_row["id"], header_left="Anna Berg", header_right="Bremen, 2026-07-31")
    vectorstore.index_file(file_row["id"])

    [hit] = [h for h in vectorstore.search("Berg") if h["source"] == "header"]
    assert hit["text"] == "Anna Berg"  # not the place, which nothing matched

    [by_name] = [h for h in vectorstore.search("protokoll") if h["source"] == "header"]
    assert by_name["text"] == "protokoll.mp3"


def test_a_date_only_match_still_shows_the_header(tmp_path):
    """`recorded_at` is searched but never shown — the header stands in."""
    file_row, _ = make_done_file(tmp_path, "d.mp3", segments=(("", "Text.", 0, 3),))
    set_header(file_row["id"], header_left="Anna Berg", recorded_at="12.05.2024")
    vectorstore.index_file(file_row["id"])

    [hit] = [h for h in vectorstore.search("12.05.2024") if h["source"] == "header"]
    assert hit["text"] == "Anna Berg"


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


# ── what is indexed: transcript, cleanup, translations ────────────────


def sources_of(file_id):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT source, source_language, text FROM chunks WHERE file_id = ? "
            "ORDER BY chunk_index",
            (file_id,),
        ).fetchall()
    return [(r["source"], r["source_language"], r["text"]) for r in rows]


def test_the_index_covers_the_derived_texts_too(tmp_path):
    """The cleanup corrects mishearings — a name is only findable there."""
    file_row, _ = make_done_file(tmp_path, segments=(("", "Amen. Martin Hummer.", 0, 3),))
    pipeline.save_text(file_row["id"], "cleanup", "Amen. Martin Humann.")
    pipeline.save_text(file_row["id"], "translation", "Amen. Martin Humann.", language="ru")
    vectorstore.index_file(file_row["id"])

    assert sources_of(file_row["id"]) == [
        ("transcript", "", "Amen. Martin Hummer."),
        ("cleanup", "", "Amen. Martin Humann."),
        ("translation", "ru", "Amen. Martin Humann."),
    ]

    hits = {h["source"] for h in vectorstore.search("Humann")}
    assert hits == {"cleanup", "translation"}  # the transcript never says it


def test_a_derived_text_hit_carries_no_timestamp(tmp_path):
    """One flowing text has no audio position — the panel is the position."""
    file_row, _ = make_done_file(tmp_path)
    pipeline.save_text(file_row["id"], "translation", "Ein Nashorn.", language="en")
    vectorstore.index_file(file_row["id"])

    [hit] = [h for h in vectorstore.search("Nashorn") if h["source"] == "translation"]
    assert (hit["start_s"], hit["end_s"]) == (0.0, 0.0)
    assert hit["source_language"] == "en"


def test_a_long_derived_text_is_split_into_searchable_passages(tmp_path):
    file_row, _ = make_done_file(tmp_path)
    paragraphs = "\n\n".join(f"Absatz {i}. " + ("Wort " * 100) for i in range(4))
    pipeline.save_text(file_row["id"], "cleanup", paragraphs)
    vectorstore.index_file(file_row["id"])

    chunks = [text for source, _, text in sources_of(file_row["id"]) if source == "cleanup"]
    assert len(chunks) > 1
    assert all(len(text) <= vectorstore.CHUNK_MAX_CHARS * 2 for text in chunks)


def test_one_paragraph_longer_than_a_chunk_is_still_split(tmp_path):
    """A cleaned-up transcript can be a single block of prose."""
    blocks = vectorstore._text_blocks("Ein Satz. " * 300, vectorstore.CHUNK_MAX_CHARS)
    assert len(blocks) > 1
    assert all(len(block) <= vectorstore.CHUNK_MAX_CHARS for block in blocks)


def test_saving_a_derived_text_enqueues_a_reindex_of_that_file(tmp_path):
    file_row, _ = make_done_file(tmp_path)
    vectorstore.index_file(file_row["id"])
    pipeline.save_text(file_row["id"], "cleanup", "Bereinigt.")

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT file_id FROM jobs WHERE kind = 'index_file' AND status = 'queued'"
        ).fetchone()
    assert row is not None and row["file_id"] == file_row["id"]


# ── a hit reports where the match is, not where its passage starts ────


def test_a_hit_reports_the_segment_the_match_is_in(tmp_path):
    """The word stands a minute in; the chunk it lives in starts at 0:00."""
    file_row, _ = make_done_file(
        tmp_path,
        segments=(
            ("", "Guten Abend allerseits.", 0.0, 30.0),
            ("", "Nun zum Nashorn.", 30.0, 60.0),
            ("", "Vielen Dank.", 60.0, 90.0),
        ),
    )
    vectorstore.index_file(file_row["id"])
    with db.get_conn() as conn:  # all three segments in one chunk
        assert conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] == 1

    [hit] = [h for h in vectorstore.search("Nashorn") if h["source"] == "transcript"]
    assert (hit["start_s"], hit["end_s"]) == (30.0, 60.0)


def test_a_purely_semantic_hit_keeps_the_start_of_its_passage():
    """With no term to locate, the beginning of the passage is the honest answer."""
    row = {
        "text": "Die Katze schläft.\nDer Hund bellt.",
        "offsets": "[[0,0.0,3.0],[19,3.0,6.0]]",
        "start_s": 0.0,
        "end_s": 6.0,
    }
    vectorstore._locate_match(row, ["nashorn"])  # nothing of the query is in it
    assert (row["start_s"], row["end_s"]) == (0.0, 6.0)

    vectorstore._locate_match(row, ["hund"])
    assert (row["start_s"], row["end_s"]) == (3.0, 6.0)


# ── the vector half has to stay quiet when nothing is close ───────────


def test_an_unrelated_file_is_not_dragged_in_by_the_vector_half(tmp_path):
    """Nearest-neighbour search always answers — on a small index, with all of it."""
    named, _ = make_done_file(
        tmp_path, "named.mp3", segments=(("", "Es sprach Martin Hummer.", 0, 5),)
    )
    other, _ = make_done_file(
        tmp_path, "other.mp3", segments=(("", "Ein Zebra läuft durch die Steppe.", 0, 5),)
    )
    vectorstore.index_file(named["id"])
    vectorstore.index_file(other["id"])

    hits = vectorstore.search("Martin")
    assert {hit["file_id"] for hit in hits} == {named["id"]}


def test_the_threshold_keeps_the_close_neighbours(tmp_path):
    """A cutoff that also silenced the semantic half would be no improvement."""
    file_row, _ = make_done_file(
        tmp_path, segments=(("", "Die Katze schläft auf dem Sofa.", 0, 5),)
    )
    vectorstore.index_file(file_row["id"])
    assert vectorstore.search("Die Katze schläft auf dem Sofa")


def test_status_flags_an_index_from_an_older_version(tmp_path):
    file_row, _ = make_done_file(tmp_path)
    vectorstore.index_file(file_row["id"])
    assert vectorstore.status()["stale_index"] is False

    with db.get_conn() as conn:
        conn.execute("UPDATE chunks SET index_version = 0")
    assert vectorstore.status()["stale_index"] is True
