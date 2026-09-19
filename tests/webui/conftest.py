"""Fixtures for the browser suite. See docs/testing/web-ui.md.

The server under test is the production `create_app`; only the manager and the host beneath it are
fakes. Frames on the wire are built by the real scope builders, so there is nothing here that can
describe the protocol differently from the application.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from review_mate.seams import MRRef
from review_mate.server.app import create_app
from webui.fixtures.host import StubHost
from webui.fixtures.manager import FakeManager
from webui.fixtures.scenarios import QUEUE

ARTIFACTS = Path(__file__).resolve().parents[2] / ".webui-artifacts"


class _Server:
    """uvicorn on an ephemeral port, in a thread, for the browser to talk to."""

    def __init__(self, app) -> None:
        self._config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> str:
        self._thread.start()
        deadline = time.time() + 20
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("the fixture server did not start")
            time.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=20)


@pytest.fixture(scope="session")
def fake_manager() -> FakeManager:
    return FakeManager()


@pytest.fixture(scope="session")
def stub_host() -> StubHost:
    return StubHost(queue=QUEUE)


@pytest.fixture(scope="session")
def staged_app(fake_manager, stub_host):
    """The production application over a manager and a host a test can set."""
    return create_app(manager=fake_manager, provider=stub_host, with_mcp=False,
                      resolve_ref=lambda raw: MRRef(host="gitlab",
                                                    project="platform/virtu/control-plane",
                                                    iid=137))


@pytest.fixture(scope="session")
def base_url(staged_app) -> str:
    server = _Server(staged_app)
    url = server.start()
    yield url
    server.stop()


@pytest.fixture(autouse=True)
def staged(fake_manager, stub_host, staged_app):
    """Reset the staged state between tests, so a scenario is the only thing a test relies on.

    The hub scope caches what it read from the host for the life of the application, so resetting
    the manager alone would leak one test's successful queue read into the next one's failure.
    """
    fake_manager.reset()
    stub_host.queue = list(QUEUE)
    stub_host.fail_with = None
    staged_app.state.hub.reset()
    yield fake_manager
    fake_manager.reset()


class Recorder:
    """What the page saw: console lines, and the frames it received."""

    def __init__(self) -> None:
        self.console: list[str] = []
        self.frames: list[dict] = []

    def attach(self, page) -> None:
        page.on("console", lambda message: self.console.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: self.console.append(f"pageerror: {error}"))
        page.on("websocket", self._watch)

    def _watch(self, socket) -> None:
        socket.on("framereceived", self._record)

    def _record(self, payload) -> None:
        try:
            self.frames.append(json.loads(payload))
        except (TypeError, ValueError):
            self.frames.append({"unparsed": str(payload)[:400]})


@pytest.fixture
def recorder(page, request) -> Recorder:
    """Attached to every page — the frame log is what tells a mis-render from a wrong view."""
    rec = Recorder()
    rec.attach(page)
    request.node.stash_recorder = rec
    return rec


@pytest.fixture(autouse=True)
def _record_everything(request):
    """Attach the recorder to any test that opens a page, and to no others.

    Requesting `page` unconditionally would parametrise every test in this directory across both
    browsers, including the ones that never open one.
    """
    if "page" not in request.fixturenames:
        return None
    return request.getfixturevalue("recorder")


def pytest_exception_interact(node, call, report):
    """On red, save a screenshot, the console, and the frames the client received."""
    if not report.failed:
        return
    page = node.funcargs.get("page") if hasattr(node, "funcargs") else None
    if page is None:
        return
    ARTIFACTS.mkdir(exist_ok=True)
    stem = node.nodeid.replace("/", "_").replace("::", "__").replace("[", "_").replace("]", "")
    try:
        page.screenshot(path=str(ARTIFACTS / f"{stem}.png"), full_page=True)
    except Exception:
        pass
    rec = getattr(node, "stash_recorder", None)
    if rec is not None:
        (ARTIFACTS / f"{stem}.console.log").write_text("\n".join(rec.console))
        (ARTIFACTS / f"{stem}.frames.json").write_text(json.dumps(rec.frames, indent=2)[:400_000])
    print(f"\nweb-ui artifacts: {ARTIFACTS}/{stem}.*")
