"""What the hub remembers about a merge request between one run of the server and the next.

A head, a lifecycle state and a count of open discussions are facts about the merge request. They
do not stop being true because the process restarted — they stop being true when the merge request
moves, which is a different thing and one a reviewer can judge for themselves if they are told how
old the answer is. Kept in memory alone, every restart made the hub claim it knew nothing about
reviews it had checked an hour earlier.
"""
import pytest

from conftest import HostStub, ref_for
from review_mate.contracts import MRRef
from review_mate.kb.store import ReviewKB
from review_mate.session.manager import SessionManager
from review_mate.view.hub import HubTopic


class Knowing(HostStub):
    """A forge that answers the two reads the hub makes."""

    def __init__(self, state="opened", head="newhead", unresolved=2, **kw):
        super().__init__(**kw)
        self._mr_state, self._head, self._unresolved = state, head, unresolved
        self.asked = 0

    async def mr_summary(self, ref):
        self.asked += 1
        return {"head": self._head, "state": self._mr_state}

    async def fetch_threads(self, ref):
        from review_mate.session.state import ReviewThread, ThreadComment
        return [ReviewThread(id=str(n), resolved=False,
                             comments=[ThreadComment(id="c", author="jsmith", body="?")])
                for n in range(self._unresolved)]


@pytest.fixture
async def running(tmp_path):
    """Two servers over one store, which is what a restart is."""
    kb = ReviewKB(root=tmp_path / "kb")

    async def start(host):
        manager = SessionManager(root=tmp_path / "sessions", mr_source=host)
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        return manager, sid, HubTopic(manager, provider=host, kb=kb)
    yield start, kb


async def _row(hub, sid):
    view = await hub.build()
    return next(s for s in view["sessions"] if s["id"] == sid), view


async def test_what_the_forge_said_outlives_the_process(running):
    start, _ = running
    host = Knowing(state="opened", unresolved=2)
    manager, sid, hub = await start(host)
    await hub.refresh()
    row, view = await _row(hub, sid)
    assert row["mr_state"] == "opened" and row["unresolved"] == 2
    assert view["host_checked_at"], "and when it was read"
    await manager.shutdown()

    # the restart: a new manager, a new hub, the same store
    manager2, sid2, hub2 = await start(Knowing())
    row2, view2 = await _row(hub2, sid2)
    assert row2["mr_state"] == "opened", "the hub forgot what the forge told it an hour ago"
    assert row2["unresolved"] == 2
    assert row2["host_checked"] is True
    await manager2.shutdown()


async def test_the_hub_still_says_when_it_last_looked(running):
    """Without it the row is a claim with no age, which is the half that makes a stale fact
    misleading rather than merely old."""
    start, _ = running
    manager, sid, hub = await start(Knowing())
    await hub.refresh()
    manager2, sid2, hub2 = await start(Knowing())
    view = await hub2.build()
    assert view["host_checked_at"], "a restarted hub reported never having checked"
    await manager.shutdown(); await manager2.shutdown()


async def test_a_fresh_read_replaces_what_was_remembered(running):
    start, _ = running
    manager, sid, hub = await start(Knowing(state="opened"))
    await hub.refresh()

    manager2, sid2, hub2 = await start(Knowing(state="merged"))
    assert (await _row(hub2, sid2))[0]["mr_state"] == "opened"   # until it is asked again
    await hub2.refresh()
    assert (await _row(hub2, sid2))[0]["mr_state"] == "merged"
    await manager.shutdown(); await manager2.shutdown()


async def test_a_branch_on_this_machine_is_not_remembered_this_way(tmp_path):
    """Its project is a directory name and its number is zero, so two branches in two clones
    would share one entry — the same trap the watermark has, and the reason it is keyed out."""
    from review_mate.contracts import LocalRef, MRPayload
    from review_mate.session.state import MRMetadata

    class LocalSource:
        async def load(self, ref):
            mr = MRMetadata(host="local", project="repo", iid=0, title="t",
                            source_branch=ref.branch, target_branch="main", sha="aa",
                            author="a", url=ref.path, clone_url=ref.path)
            return MRPayload(mr=mr, files=[], threads=[], clone_url=ref.path)

    kb = ReviewKB(root=tmp_path / "kb")
    host = Knowing()
    manager = SessionManager(root=tmp_path / "s", local_source=LocalSource())
    sid = await manager.create(ref=LocalRef(path="/w/repo", branch="feat/a", base="main"))
    hub = HubTopic(manager, provider=host, kb=kb)
    await hub.refresh()
    assert kb.get_pref("mr_facts", {}) == {}, "a branch was written into the per-merge-request store"
    await manager.shutdown()


async def test_a_second_look_in_the_same_run_replaces_the_first(running):
    """Not only across a restart. Within one run the newer answer has to win, or a review that
    was merged while you watched keeps reading as open for as long as the server stays up."""
    start, _ = running
    host = Knowing(state="opened")
    manager, sid, hub = await start(host)
    await hub.refresh()
    assert (await _row(hub, sid))[0]["mr_state"] == "opened"

    host._mr_state = "merged"
    await hub.refresh()
    assert (await _row(hub, sid))[0]["mr_state"] == "merged"
    await manager.shutdown()


async def test_a_branch_does_not_read_an_entry_left_under_its_key(tmp_path):
    """The other half of the same trap. Its project is a directory name and its number is zero,
    so an entry under `local/repo!0` belongs to whichever clone got there first — which is why a
    branch neither writes one nor believes one."""
    from review_mate.contracts import LocalRef, MRPayload
    from review_mate.session.state import MRMetadata

    class LocalSource:
        async def load(self, ref):
            mr = MRMetadata(host="local", project="repo", iid=0, title="t",
                            source_branch=ref.branch, target_branch="main", sha="aa",
                            author="a", url=ref.path, clone_url=ref.path)
            return MRPayload(mr=mr, files=[], threads=[], clone_url=ref.path)

    kb = ReviewKB(root=tmp_path / "kb")
    kb.set_mr_facts("local", "repo", 0,
                    {"head": "someone-elses", "mr_state": "merged", "unresolved": 9,
                     "checked_at": "2026-01-01T00:00:00+00:00"})
    manager = SessionManager(root=tmp_path / "s", local_source=LocalSource())
    sid = await manager.create(ref=LocalRef(path="/w/repo", branch="feat/a", base="main"))
    hub = HubTopic(manager, provider=Knowing(), kb=kb)

    row = next(s for s in (await hub.build())["sessions"] if s["id"] == sid)
    assert row["mr_state"] == "", "a branch believed a merge request's facts"
    assert row["unresolved"] == 0
    await manager.shutdown()
