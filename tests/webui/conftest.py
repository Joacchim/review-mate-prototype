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
from review_mate.session.state import Origin
from review_mate.server.app import create_app
from webui.fixtures.host import StubHost
from webui.fixtures.manager import FakeManager
from webui.fixtures.scenarios import QUEUE, StubWorkspace

ARTIFACTS = Path(__file__).resolve().parents[2] / ".webui-artifacts"


class _Server:
    """uvicorn on an ephemeral port, in a thread, for the browser to talk to."""

    def __init__(self, app) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
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
        self.loop = self._server.servers[0].get_loop()
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
def stub_workspace() -> StubWorkspace:
    return StubWorkspace()


@pytest.fixture(scope="session")
def review_kb(tmp_path_factory):
    from review_mate.kb.store import ReviewKB
    return ReviewKB(root=tmp_path_factory.mktemp("kb"))


class StubWriter:
    """The host a submitted review reaches. Records what it was sent, so a test can name it."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self.approved = False
        self.replied: list[tuple] = []
        self.resolved: list[tuple] = []
        self.edited: list[tuple] = []
        self.deleted: list[tuple] = []

    def capabilities(self) -> dict:
        from review_mate.host.base import GITLAB_CAPABILITIES
        return dict(GITLAB_CAPABILITIES)

    async def post_comment(self, ref, position, body):
        self.posted.append(body)
        return {"id": "disc-1", "notes": [{"id": 11}]}

    async def post_mr_comment(self, ref, body):
        self.posted.append(body)
        return {"id": 12}

    async def approve(self, ref):
        self.approved = True
        return {}

    async def reply(self, ref, thread_id, body):
        self.replied.append((thread_id, body))
        return {"id": "note-new"}

    async def resolve(self, ref, thread_id, resolved=True):
        self.resolved.append((thread_id, resolved))
        return {}

    async def edit_note(self, ref, thread_id, note_id, body):
        self.edited.append((thread_id, note_id, body))
        return {}

    async def delete_note(self, ref, thread_id, note_id):
        self.deleted.append((thread_id, note_id))
        return {}


@pytest.fixture(scope="session")
def stub_writer() -> StubWriter:
    return StubWriter()


@pytest.fixture(scope="session")
def staged_app(fake_manager, stub_host, stub_workspace, review_kb, stub_writer):
    """The production application over a manager, a host and a workspace a test can set."""
    from review_mate.writeback.service import Writeback
    fake_manager._workspace = stub_workspace
    return create_app(manager=fake_manager, provider=stub_host, with_mcp=False, kb=review_kb,
                      writeback=Writeback(fake_manager, stub_writer),
                      resolve_ref=lambda raw: MRRef(host="gitlab",
                                                    project="platform/virtu/control-plane",
                                                    iid=137))


@pytest.fixture(scope="session")
def _fixture_server(staged_app):
    server = _Server(staged_app)
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def base_url(_fixture_server) -> str:
    port = _fixture_server._server.servers[0].sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def as_agent(_fixture_server, fake_manager):
    """Submit a command the browser is not allowed to send — an agent emitting a card, say.

    It runs on the server's own loop, so the event reaches the session tail that is already
    subscribed and the scope republishes exactly as it would in production.
    """
    def submit(session_id: str, command, origin=Origin.AGENT):
        actor = fake_manager.actor(session_id)
        assert actor is not None, f"no staged session {session_id}"
        future = asyncio.run_coroutine_threadsafe(actor.submit(command, origin),
                                                  _fixture_server.loop)
        result = future.result(timeout=10)
        assert result.ok, result.reason
        return result
    return submit


def _await_release(app, timeout: float = 5.0) -> None:
    """Block until the bus holds no watches, so no tail outlives the test that started it."""
    deadline = time.monotonic() + timeout
    while app.state.bus.watched("") and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture
def as_claude_lookup(_fixture_server, staged_app):
    """Answer the reviewer's open lookup, the way an attached agent would.

    On the server's own loop, because answering wakes the browser's long poll through a future and
    resolving one from another thread is how a test starts passing for the wrong reason.
    """
    async def _answer(text: str, candidates):
        broker = staged_app.state.broker
        # the click and the POST it starts are not the same moment: wait for the request to arrive
        # rather than assuming it has, or this passes alone and races under load
        for _ in range(100):
            pending = [r for r in broker._by_seq if r.status == "pending"]
            if pending:
                break
            await asyncio.sleep(0.05)
        assert pending, "nothing asked Claude to find anything"
        broker.answer(pending[-1].id, text, candidates)
        return pending[-1].query        # what the reviewer actually asked Claude to find

    def answer(text: str, candidates=None):
        return asyncio.run_coroutine_threadsafe(
            _answer(text, candidates or []), _fixture_server.loop).result(timeout=10)
    return answer


@pytest.fixture(autouse=True)
def staged(fake_manager, stub_host, stub_workspace, review_kb, staged_app, stub_writer):
    """Reset the staged state between tests, so a scenario is the only thing a test relies on.

    Every scope caches what it read, for the life of the application — correctly, since content at
    a sha cannot change and a verdict holds until the host is asked again. Tests reuse one sha with
    different content, so each cache is dropped here. Resetting the manager alone leaks one test's
    file into the next test's.

    It also waits for the previous test's page to be let go of. Closing a browser page does not make
    the server notice: the socket unwinds on its own schedule, and until it does the bus still holds
    that page's watches — so a session tail started for the old test keeps running, bound to the
    actor this reset is about to throw away, and the new test gets a server that pushes it nothing.
    """
    _await_release(staged_app)
    fake_manager.reset()
    stub_host.queue = list(QUEUE)
    stub_host.search_hits = []
    stub_host.search_fails = None
    stub_host.files.clear()
    stub_host.fail_with = None
    stub_host.versions = []
    stub_host.commit_list = []
    stub_host.commit_files = {}
    stub_host.repo_tree = []
    stub_host.blame_lines = []
    stub_host.issues = []
    stub_workspace.calls = []
    stub_workspace.clean = True
    stub_writer.posted.clear()
    stub_writer.approved = False
    for recorded in (stub_writer.replied, stub_writer.resolved,
                     stub_writer.edited, stub_writer.deleted):
        recorded.clear()
    review_kb._data.watermarks = {}
    for scope in (staged_app.state.hub, staged_app.state.diff_scopes, staged_app.state.blob_scopes,
                  staged_app.state.rail_scope):
        scope.reset()
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
