"""The resumable downloader (services/download.py).

Every large file Verba fetches — a llama.cpp release, a GGUF model, its own
update artifact — goes through here, so this is where the awkward parts of a
long download are pinned down: a connection that drops halfway, a server that
answers 416 for a leftover part file, and a size limit that is never exceeded.
The `http_source` fixture in conftest.py serves the payload locally.
"""

from __future__ import annotations

import pytest

from verba.services import download

LIMIT = 12 * 1024 * 1024


def test_a_dropped_download_continues_where_it_stopped(http_source, tmp_path):
    """A model is gigabytes — a dropped connection must not restart the download."""
    # more than one read buffer, so half of it really is on disk when it drops
    http_source.handler.payload = bytes(range(256)) * 4000
    http_source.handler.drop_first = True
    target = tmp_path / "model.part"

    download.fetch(http_source.url, target, LIMIT, lambda *a: None)

    assert target.read_bytes() == http_source.handler.payload
    assert http_source.handler.ranges[0] == ""
    assert http_source.handler.ranges[1].startswith("bytes=")  # only the rest was fetched


def test_a_download_over_the_limit_is_refused(http_source, tmp_path):
    http_source.handler.payload = b"x" * 5000
    with pytest.raises(RuntimeError, match="zu groß"):
        download.fetch(http_source.url, tmp_path / "f", 100, lambda *a: None)


def test_a_stale_part_file_that_is_too_long_is_dropped(http_source, tmp_path):
    """The server answers 416 — the leftover cannot belong to this download."""
    http_source.handler.payload = b"y" * 1000
    target = tmp_path / "f.part"
    target.write_bytes(b"z" * 4000)

    download.fetch(http_source.url, target, LIMIT, lambda *a: None)

    assert target.read_bytes() == http_source.handler.payload
