"""Shared test doubles.

`HostStub` counts its own calls, so a test can assert what was *not* called — which is how the
"building a view never fans out to the host" guarantee is checked.
"""
import pytest

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
