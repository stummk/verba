"""Desktop mode follows its UI: closing the browser ends the process.

Server mode must never do that — it keeps running until its service is
stopped.
"""

from __future__ import annotations

import asyncio

import pytest

from verba import lifecycle
from verba.events import hub


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch):
    monkeypatch.setenv("VERBA_IDLE_EXIT_SECONDS", "0.05")
    yield
    lifecycle.cancel_idle_watchdog()


def test_desktop_mode_needs_the_env_flag(monkeypatch):
    assert lifecycle.desktop_mode() is False
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "1")
    assert lifecycle.desktop_mode() is True
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "0")
    assert lifecycle.desktop_mode() is False


def test_idle_exit_seconds_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("VERBA_IDLE_EXIT_SECONDS", "soon")
    assert lifecycle.idle_exit_seconds() == lifecycle.IDLE_EXIT_SECONDS
    monkeypatch.setenv("VERBA_IDLE_EXIT_SECONDS", "0")
    assert lifecycle.idle_exit_seconds() == 0.0


def test_server_mode_keeps_running_without_a_ui(monkeypatch):
    monkeypatch.delenv("VERBA_DESKTOP_MODE", raising=False)
    stopped = _record_stop(monkeypatch)

    async def scenario():
        lifecycle.arm_idle_watchdog()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert stopped == []


def test_desktop_mode_stops_when_the_ui_stays_away(monkeypatch):
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "1")
    stopped = _record_stop(monkeypatch)

    async def scenario():
        lifecycle.arm_idle_watchdog()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert stopped == [True]


def test_a_reload_cancels_the_pending_shutdown(monkeypatch):
    """A page reload drops the WebSocket and reconnects a moment later."""
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "1")
    stopped = _record_stop(monkeypatch)

    async def scenario():
        lifecycle.arm_idle_watchdog()
        await asyncio.sleep(0.01)
        lifecycle.cancel_idle_watchdog()  # the UI is back
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert stopped == []


def test_a_still_connected_client_prevents_the_shutdown(monkeypatch):
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "1")
    stopped = _record_stop(monkeypatch)
    monkeypatch.setattr(type(hub), "client_count", property(lambda self: 1))

    async def scenario():
        lifecycle.arm_idle_watchdog()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert stopped == []


def test_the_watchdog_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "1")
    monkeypatch.setenv("VERBA_IDLE_EXIT_SECONDS", "0")
    stopped = _record_stop(monkeypatch)

    async def scenario():
        lifecycle.arm_idle_watchdog()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert stopped == []


def test_the_last_websocket_arms_the_watchdog(client, monkeypatch):
    """Wiring check through the app: the UI leaving arms the shutdown, a
    second still-open UI does not."""
    armed: list[str] = []
    monkeypatch.setattr(lifecycle, "arm_idle_watchdog", lambda: armed.append("armed"))
    monkeypatch.setattr(lifecycle, "cancel_idle_watchdog", lambda: armed.append("cancelled"))

    with client.websocket_connect("/ws"):
        with client.websocket_connect("/ws"):
            pass
        assert hub.client_count == 1
        assert armed == ["cancelled", "cancelled"]  # no shutdown while one is left

    assert hub.client_count == 0
    assert armed[-1] == "armed"


def _record_stop(monkeypatch) -> list[bool]:
    stopped: list[bool] = []
    monkeypatch.setattr(lifecycle, "stop_process", lambda delay=0.1: stopped.append(True))
    return stopped
