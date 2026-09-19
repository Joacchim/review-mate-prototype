"""The second client against a real server over a real socket.

This is the seam's proof: the TUI renders reviews it never modelled, and acts on them through
the same command path the browser uses. If the view protocol were browser-shaped, it would show
up here first.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest
import uvicorn

from conftest import HostStub
from review_mate.seams import MRRef
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.tui.app import HubScreen
from review_mate.tui.client import ViewClient

pytest.importorskip("websockets")


@asynccontextmanager
async def serving(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    deadline = asyncio.get_running_loop().time() + 10
    while not server.started:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("server did not start")
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


class Watcher:
    """Waits for a view satisfying a predicate, driven by the client's own callback."""

    def __init__(self, client: ViewClient):
        self.client = client
        self._predicate = None
        self._event = asyncio.Event()

    def notify(self) -> None:
        view = self.client.views.get("hub")
        if self._predicate is not None and view is not None and self._predicate(view):
            self._event.set()

    async def until(self, predicate, timeout=10.0) -> dict:
        self._predicate = predicate
        self._event.clear()
        self.notify()
        await asyncio.wait_for(self._event.wait(), timeout)
        return self.client.views["hub"]


@asynccontextmanager
async def connected(base_url):
    client = ViewClient(base_url)
    watcher = Watcher(client)
    stream = asyncio.create_task(client.run(["hub"], watcher.notify))
    try:
        yield client, watcher
    finally:
        stream.cancel()
        try:
            await stream
        except asyncio.CancelledError:
            pass


def build(tmp_path, provider):
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    return create_app(manager=manager, provider=provider, with_mcp=False,
                      resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))


async def test_the_tui_client_renders_a_hub_it_never_modelled(tmp_path):
    provider = HostStub()
    async with serving(build(tmp_path, provider)) as base:
        async with connected(base) as (client, watcher):
            view = await watcher.until(lambda v: v.get("queue_state") == "ready")
            assert client.status == "live"
            assert view["user"] == "reviewer"
            assert view["queue"][0]["iid"] == 7
            assert view["sessions"] == []


async def test_the_tui_acts_through_the_same_command_path(tmp_path):
    provider = HostStub()
    async with serving(build(tmp_path, provider)) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")

            ok = await client.command("session.open",
                                      ref={"host": "gitlab", "project": "g/p", "iid": 1})
            assert ok, client.last_command_error
            view = await watcher.until(lambda v: v["sessions"])
            sid = view["sessions"][0]["id"]
            assert view["sessions"][0]["host_checked"] is False

            assert await client.command("hub.refresh"), client.last_command_error
            view = await watcher.until(lambda v: v["sessions"] and v["sessions"][0]["host_checked"])
            assert view["sessions"][0]["unresolved"] == 1
            assert view["sessions"][0]["state"] == "discussions"

            assert await client.command("session.close", id=sid), client.last_command_error
            view = await watcher.until(lambda v: not v["sessions"])
            assert view["sessions"] == []


async def test_a_rejected_command_reports_why(tmp_path):
    async with serving(build(tmp_path, HostStub())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            assert await client.command("session.close", id="ghost") is False
            assert "unknown session" in client.last_command_error


async def test_the_rendered_screen_shows_what_the_server_sent(tmp_path):
    """Protocol and rendering together: the text a reviewer would actually read."""
    async with serving(build(tmp_path, HostStub())) as base:
        async with connected(base) as (client, watcher):
            await watcher.until(lambda v: v.get("queue_state") == "ready")
            screen = HubScreen(client)
            rendered = "".join(text for _, text in screen.fragments())
            assert "review-mate · reviewer" in rendered
            assert "g/p!7  queued" in rendered
            assert "none" in rendered                    # no open reviews yet
            assert "j/k move" in rendered
