"""Test fixtures: every test runs against an isolated temporary data dir."""

from __future__ import annotations

import http.server
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from verba import config
from verba.services import download


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VERBA_DATA_DIR", str(tmp_path / "data"))
    # desktop mode ends the process when the last UI disconnects — never
    # inherit it from the developer's shell into a test run
    monkeypatch.delenv("VERBA_DESKTOP_MODE", raising=False)
    config.reset_cache()
    yield
    config.reset_cache()


@pytest.fixture()
def client():
    from verba.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves one payload, understands Range, and can drop the first attempt."""

    payload = b""
    drop_first = False
    ranges: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        cls = type(self)
        header = self.headers.get("Range", "")
        cls.ranges.append(header)
        start = int(header.split("=")[1].split("-")[0]) if header else 0
        if start >= len(cls.payload):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = cls.payload[start:]
        if start:
            self.send_response(206)
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(cls.payload) - 1}/{len(cls.payload)}",
            )
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if cls.drop_first and len(cls.ranges) == 1:
            self.wfile.write(body[: len(body) // 2])  # the connection closes short
            return
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def http_source(monkeypatch):
    """A local HTTP server plus the knobs these tests turn.

    Shared, because more than one service downloads: the llama.cpp
    installation and the app's own update both go through services/download.py
    and both have to be tested against a server that can drop a connection.
    """
    monkeypatch.setattr(download, "RETRY_DELAY_S", 0)
    _RangeHandler.payload = b""
    _RangeHandler.drop_first = False
    _RangeHandler.ranges = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_address[1]}/file",
            handler=_RangeHandler,
        )
    finally:
        server.shutdown()
        server.server_close()
