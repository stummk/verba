from __future__ import annotations

from verba.services.chunking import chunk_segments
from verba.services.pipeline import _chunk_text


def seg(idx: int, text: str) -> dict:
    return {"idx": idx, "start_s": idx * 2.0, "end_s": idx * 2.0 + 2.0, "text": text}


def test_short_input_is_one_chunk():
    chunks = chunk_segments([seg(0, "hallo"), seg(1, "welt")], max_chars=1000)
    assert len(chunks) == 1
    assert chunks[0].own_start == 0
    assert chunks[0].own_text == "hallo\nwelt"
    assert chunks[0].context_text == ""


def test_chunks_split_on_segment_boundaries():
    segments = [seg(i, "wort " * 20) for i in range(10)]
    chunks = chunk_segments(segments, max_chars=250, overlap=0)
    assert len(chunks) > 1
    total = sum(len(c.segments) for c in chunks)
    assert total == 10  # no segment lost, none duplicated (overlap=0)


def test_overlap_segments_are_context_only():
    segments = [seg(i, f"segment-{i} " + "x" * 50) for i in range(6)]
    chunks = chunk_segments(segments, max_chars=150, overlap=2)
    assert len(chunks) >= 2
    second = chunks[1]
    assert second.own_start == 2
    assert "segment-" in second.context_text
    # own segments across all chunks cover every segment exactly once
    own = [s["idx"] for c in chunks for s in c.segments[c.own_start :]]
    assert own == list(range(6))


def test_empty_input():
    assert chunk_segments([]) == []


def test_chunk_text_splits_on_paragraphs():
    text = "\n\n".join(f"Absatz {i}: " + "wort " * 30 for i in range(10))
    parts = _chunk_text(text, max_chars=400)
    assert len(parts) > 1
    assert all(len(p) <= 400 for p in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(
        " ", ""
    )


def test_chunk_text_short_text_untouched():
    assert _chunk_text("kurz", max_chars=100) == ["kurz"]
