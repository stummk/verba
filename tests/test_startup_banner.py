"""The address block printed on start.

start.sh / start.bat show it in the console, systemd captures it in the
journal — so "which port is it on?" never needs the unit file. The socket is
bound before the banner is printed, because a banner must not promise an
address that a port conflict then denies.
"""

from __future__ import annotations

import socket

import pytest
import run

from verba import __version__


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ── the banner ────────────────────────────────────────────────────────


def test_server_mode_names_every_reachable_address(monkeypatch):
    monkeypatch.setattr(run, "local_addresses", lambda: ["192.168.1.50", "2001:db8::7"])

    banner = run.startup_banner("0.0.0.0", 8710, True, "/opt/verba/data")

    assert f"Verba {__version__}" in banner
    assert "server mode" in banner
    assert "http://0.0.0.0:8710" in banner
    assert "all interfaces" in banner
    assert "http://127.0.0.1:8710" in banner
    assert "http://192.168.1.50:8710" in banner
    assert "http://[2001:db8::7]:8710" in banner  # IPv6 needs the brackets
    assert "/opt/verba/data" in banner


def test_desktop_mode_names_the_loopback_it_actually_binds():
    banner = run.startup_banner("127.0.0.1", 8710, False, "/home/u/verba/data")
    assert "desktop mode" in banner
    assert "http://localhost:8710" in banner
    assert "127.0.0.1 and [::1]" in banner


def test_a_specific_host_is_shown_as_it_is(monkeypatch):
    monkeypatch.setattr(run, "local_addresses", lambda: ["192.168.1.50"])
    banner = run.startup_banner("10.0.0.5", 9000, True, "/data")
    assert "http://10.0.0.5:9000" in banner
    assert "all interfaces" not in banner
    assert "192.168.1.50" not in banner  # not bound there, so not promised


def test_the_port_is_the_configured_one():
    assert "http://0.0.0.0:9999" in run.startup_banner("0.0.0.0", 9999, True, "/data")


def test_the_banner_is_framed_by_a_rule_of_its_own_width():
    lines = run.startup_banner("0.0.0.0", 8710, True, "/data").split("\n")
    assert lines[0] == lines[-1]
    assert set(lines[0]) == {"-"}
    assert all(len(line) <= len(lines[0]) for line in lines)


def test_the_banner_is_pure_ascii():
    """A Windows console pipe may be cp1252 or cp437 — box drawing and even an
    em dash raise UnicodeEncodeError there, which would kill the start."""
    run.startup_banner("0.0.0.0", 8710, True, "/opt/verba/data").encode("ascii")


# ── address discovery ─────────────────────────────────────────────────


def test_local_addresses_skips_loopback_and_link_local():
    for address in run.local_addresses():
        assert not address.startswith(("127.", "::1", "fe80"))
        assert address == address.strip()


def test_local_addresses_survives_a_broken_resolver(monkeypatch):
    """A server whose hostname does not resolve must still start."""

    def boom(*args, **kwargs):
        raise OSError("name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert isinstance(run.local_addresses(), list)  # no exception


# ── binding ───────────────────────────────────────────────────────────


def test_the_loopback_bind_covers_both_families():
    port = free_port()
    sockets = run.bind_sockets("127.0.0.1", port)
    try:
        families = {sock.family for sock in sockets}
        assert socket.AF_INET in families
        assert all(sock.getsockname()[1] == port for sock in sockets)
    finally:
        for sock in sockets:
            sock.close()


def test_an_occupied_port_exits_with_one_clear_line():
    port = free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", port))
    try:
        with pytest.raises(SystemExit) as exit_info:
            run.bind_sockets("127.0.0.1", port)
        assert str(port) in str(exit_info.value)
        assert "already in use" in str(exit_info.value)
    finally:
        blocker.close()


def test_an_unusable_host_exits_instead_of_raising():
    with pytest.raises(SystemExit) as exit_info:
        run.bind_sockets("203.0.113.7", free_port())  # not an address of this machine
    assert "Cannot listen on 203.0.113.7" in str(exit_info.value)


def test_a_wildcard_bind_returns_one_listening_socket():
    port = free_port()
    [sock] = run.bind_sockets("0.0.0.0", port)
    try:
        assert sock.getsockname() == ("0.0.0.0", port)
    finally:
        sock.close()


# ── the log line ──────────────────────────────────────────────────────


def test_the_app_logs_the_bound_address(monkeypatch, caplog):
    """The same information in the rotating file log, for a service that has
    been running for weeks."""
    monkeypatch.setenv("VERBA_BIND", "0.0.0.0:8710")
    from fastapi.testclient import TestClient

    from verba.main import create_app

    with caplog.at_level("INFO"), TestClient(create_app()):
        pass

    assert "listening on 0.0.0.0:8710" in caplog.text


def test_the_log_line_is_honest_without_the_address(monkeypatch, caplog):
    monkeypatch.delenv("VERBA_BIND", raising=False)
    from fastapi.testclient import TestClient

    from verba.main import create_app

    with caplog.at_level("INFO"), TestClient(create_app()):
        pass

    assert "address unknown" in caplog.text
