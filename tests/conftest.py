"""Test fixtures: every test runs against an isolated temporary data dir."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from verba import config


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
