"""The `tree` and `commits` topics: the repository around the change, and how it got here.

Both cost a host read, and both answer a question a reviewer asks only sometimes. So the thing
worth pinning is not the content but the gating: building never asks, the fetch asks once, and a
view says `loading` while the answer is on its way rather than blocking on it.
"""
import pytest

from conftest import HostStub
from review_mate.contracts import MRRef
from review_mate.session.commands import ApplyMRMetadata
from review_mate.session.manager import SessionManager
from review_mate.session.state import Origin
from review_mate.view.browse import BrowseTopics


class BrowsableHost(HostStub):
    """A host that can list a repository and a change's commits, and counts being asked."""

    def __init__(self, paths=("a.py", "pkg/b.py"), commits=None, fail=None, **kwargs):
        super().__init__(**kwargs)
        self.tree_calls = 0
        self.commit_calls = 0
        self._paths = list(paths)
        self._commits = list(commits or [{"sha": "s1", "short_id": "s1", "title": "first",
                                          "message": "first", "author": "dev", "created_at": ""}])
        self._fail = fail

    async def load(self, ref: MRRef):
        payload = await super().load(ref)
        payload.mr.capabilities = {"commits": True}
        return payload

    async def get_repo_tree(self, project, ref, max_pages=30):
        self.tree_calls += 1
        if self._fail == "tree":
            raise RuntimeError("host is down")
        return list(self._paths)

    async def commits(self, ref: MRRef):
        self.commit_calls += 1
        if self._fail == "commits":
            raise RuntimeError("host is down")
        return list(self._commits)


@pytest.fixture
async def browse(tmp_path):
    published = []

    async def build(host=None):
        host = host if host is not None else BrowsableHost()
        manager = SessionManager(root=tmp_path / "sessions", mr_source=host)
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        topics = BrowseTopics(manager, provider=host,
                             publish=lambda topic: _record(published, topic))
        return manager, sid, topics, host, published
    yield build


async def _record(published, topic):
    published.append(topic)


# --- the repository's files ---------------------------------------------------

async def test_the_tree_is_not_read_until_someone_looks(browse):
    manager, sid, topics, host, _ = await browse()
    view = await topics.build_tree(sid)
    assert view["state"] == "idle" and view["paths"] == []
    assert host.tree_calls == 0, "building must not reach the host"
    await manager.shutdown()


async def test_fetching_it_fills_the_view_and_republishes(browse):
    manager, sid, topics, host, published = await browse()
    await topics.fetch_tree(sid)
    view = await topics.build_tree(sid)
    assert view["state"] == "ready" and view["paths"] == ["a.py", "pkg/b.py"]
    assert host.tree_calls == 1
    # loading, then ready — a reviewer sees it is on its way rather than nothing at all
    assert published == [f"tree:{sid}", f"tree:{sid}"]
    await manager.shutdown()


async def test_a_tree_at_a_sha_is_read_once(browse):
    """Contents at a sha cannot change, so what is cached never needs invalidating."""
    manager, sid, topics, host, _ = await browse()
    await topics.fetch_tree(sid)
    await topics.fetch_tree(sid)
    assert host.tree_calls == 1
    await manager.shutdown()


async def test_a_host_that_cannot_list_a_repository_says_unavailable(browse):
    manager, sid, topics, _host, _ = await browse(host=HostStub())   # no get_repo_tree
    assert (await topics.build_tree(sid))["state"] == "unavailable"
    await manager.shutdown()


async def test_a_failed_read_is_reported_in_the_view_not_raised(browse):
    manager, sid, topics, _host, _ = await browse(host=BrowsableHost(fail="tree"))
    await topics.fetch_tree(sid)
    view = await topics.build_tree(sid)
    assert view["state"] == "error" and "host is down" in view["error"]
    await manager.shutdown()


# --- the change's commits -----------------------------------------------------

async def test_the_commits_are_not_read_until_someone_looks(browse):
    manager, sid, topics, host, _ = await browse()
    assert (await topics.build_commits(sid))["state"] == "idle"
    assert host.commit_calls == 0
    await manager.shutdown()


async def test_fetching_them_fills_the_view(browse):
    manager, sid, topics, host, _ = await browse()
    await topics.fetch_commits(sid)
    view = await topics.build_commits(sid)
    assert view["state"] == "ready"
    assert [c["sha"] for c in view["commits"]] == ["s1"]
    assert host.commit_calls == 1
    await manager.shutdown()


async def test_an_mr_whose_host_cannot_list_commits_says_unavailable(browse):
    manager, sid, topics, host, _ = await browse()
    snapshot = manager.get(sid).snapshot()
    await manager.get(sid).submit(
        ApplyMRMetadata(mr=snapshot.mr.model_copy(update={"capabilities": {}})), Origin.SYSTEM)
    assert (await topics.build_commits(sid))["state"] == "unavailable"
    assert host.commit_calls == 0
    await manager.shutdown()


async def test_a_head_that_moved_drops_what_was_read(browse):
    """The list belongs to a head, so a re-sync that moves it must not leave the old one showing."""
    manager, sid, topics, host, _ = await browse()
    await topics.fetch_commits(sid)
    assert (await topics.build_commits(sid))["state"] == "ready"

    topics.forget_commits(sid)
    assert (await topics.build_commits(sid))["state"] == "idle"
    await topics.fetch_commits(sid)
    assert host.commit_calls == 2
    await manager.shutdown()


async def test_a_session_that_is_not_there_says_unknown_session(browse):
    manager, _sid, topics, _host, _ = await browse()
    assert (await topics.build_tree("nope"))["state"] == "unknown-session"
    assert (await topics.build_commits("nope"))["state"] == "unknown-session"
    await manager.shutdown()
