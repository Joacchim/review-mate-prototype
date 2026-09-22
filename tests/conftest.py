"""Shared test doubles, and the guard that keeps the suite out of the reviewer's own files.

`HostStub` counts its own calls, so a test can assert what was *not* called — which is how the
"building a view never fans out to the host" guarantee is checked.
"""
import pytest

from review_mate.config import review_mate_home


@pytest.fixture(autouse=True)
def _home_is_never_the_reviewers(tmp_path_factory, monkeypatch):
    """No test writes into `~/.review-mate`.

    Every default path resolves through `review_mate_home()`, so pointing that at a temporary
    directory covers the knowledge base, the session log and the workspace at once. Doing it here
    rather than per test is the point: `create_app` defaults its knowledge base to the real home,
    and a test that submits a review writes a watermark — so one call site forgetting to pass a
    root is enough to edit the reviewer's own review history.
    """
    monkeypatch.setenv("REVIEW_MATE_HOME", str(tmp_path_factory.mktemp("home")))

from review_mate.seams import MRPayload, MRRef
from review_mate.session.state import ChangeType, FileEntry, MRMetadata, ReviewThread


class HostStub:
    username = "reviewer"

    def __init__(self, queue=None, queue_error=None, unresolved=1, state="opened"):
        self.calls = {"queue": 0, "summary": 0, "threads": 0}
        self._queue = queue if queue is not None else [
            {"host": "gitlab", "project": "g/p", "iid": 7, "title": "queued", "url": "http://q"}]
        self._queue_error = queue_error
        self._unresolved = unresolved
        self._state = state

    async def load(self, ref: MRRef) -> MRPayload:
        return MRPayload(
            mr=MRMetadata(host="gitlab", project=ref.project, iid=ref.iid, title="T",
                          source_branch="x", target_branch="main", sha="abc",
                          author="dev", url="http://x"),
            files=[FileEntry(path="a.py", change_type=ChangeType.MODIFIED)],
            threads=[],
        )

    async def review_queue_items(self):
        self.calls["queue"] += 1
        if self._queue_error is not None:
            raise self._queue_error
        return self._queue

    async def mr_summary(self, ref: MRRef):
        self.calls["summary"] += 1
        return {"head": "abc", "state": self._state}

    async def fetch_threads(self, ref: MRRef):
        self.calls["threads"] += 1
        return [ReviewThread(id=str(n), resolved=False) for n in range(self._unresolved)]


@pytest.fixture
def host_stub():
    """The stub class itself, so a test can construct it with the host behaviour it needs."""
    return HostStub


def pytest_configure(config):
    """Default the browser suite to every browser it claims to cover.

    pytest-playwright runs chromium alone unless told otherwise, so a bare `pytest` would quietly
    cover half of what docs/testing/web-ui.md promises. `--browser` still narrows it.
    """
    option = getattr(config.option, "browser", None)
    if option is not None and not option:
        config.option.browser = ["chromium", "firefox"]


def next_frame(ws, timeout: float = 2.0):
    """The next frame on a TestClient websocket, or None if none arrives in time.

    `ws.receive_text()` blocks forever, so a test asserting that something arrives hangs rather
    than fails when it does not — and since the bus withholds a view that did not change, "nothing
    arrives" is a real outcome to assert on. This is `receive()` with a deadline, run on the portal
    the session already uses.
    """
    import json

    import anyio

    async def _read():
        with anyio.move_on_after(timeout):
            return await ws._send_rx.receive()
        return None

    message = ws.portal.call(_read)
    return json.loads(message["text"]) if message else None
