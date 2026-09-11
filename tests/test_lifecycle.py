"""Desktop mode follows its UI: closing the browser ends the process.

Server mode must never do that — it keeps running until its service is
stopped. And whichever way it ends, the models go out of memory first.
"""

from __future__ import annotations

import asyncio
import signal

import pytest
from fastapi.testclient import TestClient

from verba import lifecycle
from verba.events import hub
from verba.services import llamacpp, vectorstore, whisper


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch):
    monkeypatch.setenv("VERBA_IDLE_EXIT_SECONDS", "0.05")
    # module state of a process that is supposed to keep running
    monkeypatch.setattr(lifecycle, "_server", None)
    monkeypatch.setattr(lifecycle, "_exiting", False)
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


class _FakeServer:
    """As much of uvicorn's Server as the shutdown touches."""

    def __init__(self) -> None:
        self.should_exit = False


def test_the_exit_goes_through_the_server_not_through_a_signal(monkeypatch):
    """On Windows `os.kill` is TerminateProcess: the shutdown would never run,
    and every model would be left in memory."""
    server = _FakeServer()
    lifecycle.bind_server(server)
    killed: list[int] = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: killed.append(sig))

    lifecycle._request_exit()

    assert server.should_exit is True
    assert killed == []


def test_without_a_bound_server_the_signal_remains(monkeypatch):
    """A foreign ASGI host (tests, uvicorn started by hand) never binds one."""
    killed: list[int] = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: killed.append(sig))

    lifecycle._request_exit()

    assert killed == [signal.SIGTERM]


def test_a_leaving_ui_no_longer_arms_a_process_on_its_way_out(monkeypatch):
    """The shutdown closes the UI sockets; that must not schedule a watchdog
    into a loop that is about to be gone."""
    monkeypatch.setenv("VERBA_DESKTOP_MODE", "1")
    lifecycle.bind_server(_FakeServer())
    armed: list[object] = []

    async def scenario():
        lifecycle.stop_process(delay=0)
        lifecycle.arm_idle_watchdog()
        armed.append(lifecycle._watchdog)

    asyncio.run(scenario())
    assert armed == [None]


def test_release_frees_every_engine(monkeypatch):
    released = _record_releases(monkeypatch)
    lifecycle.release_local_models()
    assert released == ["llama-server", "whisper", "embeddings"]


def test_one_engine_failing_does_not_keep_the_others_loaded(monkeypatch):
    released = _record_releases(monkeypatch)

    def boom() -> None:
        raise RuntimeError("the process is gone already")

    monkeypatch.setattr(llamacpp, "stop_server", boom)

    lifecycle.release_local_models()

    assert released == ["whisper", "embeddings"]


def test_shutting_down_the_app_releases_the_models(monkeypatch):
    """The wiring: whatever ends the server, the lifespan gives the memory back."""
    from verba.main import create_app

    released = _record_releases(monkeypatch)

    with TestClient(create_app()):
        assert released == []

    assert released == ["llama-server", "whisper", "embeddings"]


def _record_releases(monkeypatch) -> list[str]:
    released: list[str] = []
    monkeypatch.setattr(llamacpp, "stop_server", lambda: released.append("llama-server"))
    monkeypatch.setattr(whisper, "unload_model", lambda: released.append("whisper"))
    monkeypatch.setattr(vectorstore, "unload_model", lambda: released.append("embeddings"))
    return released


def _record_stop(monkeypatch) -> list[bool]:
    stopped: list[bool] = []
    monkeypatch.setattr(lifecycle, "stop_process", lambda delay=0.1: stopped.append(True))
    return stopped
