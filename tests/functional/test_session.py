"""Functional tests for the SessionWriter + SessionManager (in-process, tmp store).

Covers the lifecycle and the live round-trip: AC-1, 2, 3, 4, 5, 9, 10.
"""
import asyncio
import pytest

from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight, EmitCard, EndSession
from review_mate.session.state import Origin, Side, LineRange, SessionStatus


@pytest.fixture
async def manager(tmp_path):
    m = SessionManager(root=tmp_path / "sessions")
    yield m
    await m.shutdown()


async def test_load_materializes_checkout_and_end_releases(tmp_path):
    """Eager checkout: create(ref) materializes an on-disk worktree of the MR and exposes its path
    as checkout_path; ending the session releases it. Best-effort — a workspace failure won't sink
    the load (covered by the guard in _materialize_checkout)."""
    from review_mate.contracts import MRRef, MRPayload, CheckoutHandle
    from review_mate.session.state import MRMetadata

    calls = {}

    class FakeSource:
        async def load(self, ref):
            mr = MRMetadata(host="gitlab", project="g/p", iid=1, title="t", source_branch="x",
                            target_branch="m", sha="deadbeef", author="a", url="u",
                            clone_url="git@h:g/p.git")
            return MRPayload(mr=mr, files=[], threads=[], clone_url="git@h:g/p.git")

    class FakeWorkspace:
        async def materialize(self, repo, commit):
            calls["materialized"] = (repo.project, commit)
            return CheckoutHandle(repo=repo.project, commit=commit, path="/tmp/co/g_p")

        async def release(self, handle):
            calls["released"] = handle.path

    m = SessionManager(root=tmp_path / "s", mr_source=FakeSource(), workspace=FakeWorkspace())
    sid = await m.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
    assert m.get(sid).snapshot().checkout_path == "/tmp/co/g_p"
    assert calls["materialized"] == ("g/p", "deadbeef")
    await m.end(sid)
    assert calls["released"] == "/tmp/co/g_p"
    await m.shutdown()


def _add(file="a.py", lo=1, hi=2):
    return AddHighlight(file=file, side=Side.NEW, line_range=LineRange(start=lo, end=hi))


async def test_create_session_is_active(manager):  # AC-1
    sid = await manager.create()
    writer = manager.get(sid)
    assert writer is not None
    assert writer.snapshot().status is SessionStatus.ACTIVE


async def test_unknown_session_returns_none(manager):  # AC-10
    assert manager.get("nope") is None


async def test_mutation_applies_and_pushes_to_subscriber(manager):  # AC-2
    sid = await manager.create()
    writer = manager.get(sid)
    stream = writer.subscribe(since=writer.snapshot().seq).__aiter__()
    res = await writer.submit(_add(), Origin.BROWSER)
    assert res.ok
    evt = await asyncio.wait_for(stream.__anext__(), 1)
    assert evt.type == "highlight_added"
    assert len(writer.snapshot().highlights) == 1


async def test_subscribe_replays_history_then_live(manager):  # AC-3
    sid = await manager.create()
    writer = manager.get(sid)
    await writer.submit(_add(file="past.py"), Origin.BROWSER)
    stream = writer.subscribe(since=0).__aiter__()
    # past events replayed (session_created + the highlight)
    types = [(await asyncio.wait_for(stream.__anext__(), 1)).type for _ in range(2)]
    assert "highlight_added" in types
    # then a live one
    await writer.submit(_add(file="live.py"), Origin.BROWSER)
    live = await asyncio.wait_for(stream.__anext__(), 1)
    assert live.type == "highlight_added"
    assert live.highlight.file == "live.py"


async def test_sessions_are_isolated(manager):  # AC-4
    a = manager.get(await manager.create())
    b = manager.get(await manager.create())
    await a.submit(_add(), Origin.BROWSER)
    assert len(a.snapshot().highlights) == 1
    assert len(b.snapshot().highlights) == 0


async def test_a_session_rejects_writes_once_ended(manager):  # AC-5
    """The event alone closes the session to writes, before anything is cleared away."""
    sid = await manager.create()
    writer = manager.get(sid)
    await writer.submit(EndSession(), Origin.BROWSER)
    assert writer.snapshot().status is SessionStatus.ENDED
    res = await writer.submit(_add(), Origin.BROWSER)
    assert not res.ok  # no mutation after end


async def test_ending_a_session_clears_what_it_held(manager):  # AC-5
    """Ending is not a status change — the stored session is deleted.

    What was posted is on the host, which is the durable record of a review. What was not was
    working material, and keeping it grows the store by one review for every session ever opened,
    each of them restored again at every boot.
    """
    sid = await manager.create()
    assert (manager.root / sid).exists()
    await manager.end(sid)
    assert manager.get(sid) is None
    assert not (manager.root / sid).exists()
    assert [s.id for s in manager.list()] == []


async def test_a_session_ended_by_an_older_version_is_not_restored(tmp_path):
    """Sessions ended before this behaviour existed are still on disk, and an interrupted purge
    leaves one too. Neither is live, so neither is loaded — otherwise the store keeps answering
    for reviews that are over."""
    root = tmp_path / "s"
    first = SessionManager(root=root)
    sid = await first.create()
    first._write_meta(root / sid, sid, first.get(sid).snapshot().created_at, SessionStatus.ENDED)
    await first.shutdown()

    second = SessionManager(root=root)
    await second.restore_all()
    assert second.get(sid) is None
    await second.shutdown()


async def test_authority_rejection_surfaces(manager):  # AC-9 (authority at the writer)
    sid = await manager.create()
    writer = manager.get(sid)
    res = await writer.submit(EmitCard(highlight_id="x", body="b"), Origin.BROWSER)
    assert not res.ok and res.reason


async def test_concurrent_writes_serialized_none_lost(manager):  # AC-9 (serialization)
    sid = await manager.create()
    writer = manager.get(sid)
    n = 50
    results = await asyncio.gather(
        *(writer.submit(_add(file=f"f{i}.py"), Origin.BROWSER) for i in range(n))
    )
    assert all(r.ok for r in results)
    seqs = sorted(r.seq for r in results)
    assert seqs == list(range(seqs[0], seqs[0] + n))  # contiguous, unique — nothing lost
    assert len(writer.snapshot().highlights) == n


class ReleaseRecordingWorkspace:
    """Records what it was asked to release, and hands out a worktree per sha."""

    def __init__(self):
        self.released = []

    async def materialize(self, repo, commit):
        from review_mate.contracts import CheckoutHandle
        return CheckoutHandle(repo=repo.project, commit=commit, path=f"/wt/{commit}")

    async def release(self, handle):
        self.released.append(handle.path)


async def test_a_checkout_made_before_a_restart_is_still_released(tmp_path):
    """The handle that can remove a worktree lives in memory, so a session that outlives the server
    process used to lose it — and ending that session afterwards freed nothing. That is how the
    checkouts area grows without bound while eviction looks implemented.
    """
    from review_mate.contracts import MRPayload
    from review_mate.session.state import MRMetadata

    mr = MRMetadata(host="gitlab", project="g/p", iid=42, title="T", source_branch="x",
                    target_branch="main", sha="abc123", author="a", url="u",
                    clone_url="https://gl/g/p.git")

    class Provider:
        async def load(self, ref):
            return MRPayload(mr=mr, files=[], threads=[], clone_url="https://gl/g/p.git")
        async def fetch_threads(self, ref):
            return []

    root = tmp_path / "s"
    from review_mate.contracts import MRRef
    first = SessionManager(root=root, workspace=ReleaseRecordingWorkspace(), mr_source=Provider())
    sid = await first.create()
    await first.load(sid, MRRef(host="gitlab", project="g/p", iid=42))
    assert first.get(sid).snapshot().checkout_path == "/wt/abc123"
    await first.shutdown()                       # the process goes away; the worktree does not

    workspace = ReleaseRecordingWorkspace()
    second = SessionManager(root=root, workspace=workspace, mr_source=Provider())
    await second.restore_all()
    await second.end(sid)
    assert workspace.released == ["/wt/abc123"]
    await second.shutdown()


async def test_a_borrowed_working_repository_is_never_released(tmp_path):
    """A local branch's checkout_path is the reviewer's own clone, not a worktree we made. Adopting
    it on restore would delete their repository when the session ends."""
    from review_mate.contracts import LocalRef, MRPayload
    from review_mate.session.state import MRMetadata

    repo = tmp_path / "theirs"
    repo.mkdir()
    mr = MRMetadata(host="local", project="theirs", iid=0, title="T", source_branch="x",
                    target_branch="main", sha="abc123", author="a", url=str(repo),
                    clone_url=str(repo))

    class Local:
        host = "local"
        async def load(self, ref):
            return MRPayload(mr=mr, files=[], threads=[], clone_url=str(repo),
                             checkout_path=str(repo))
        async def fetch_threads(self, ref):
            return []

    root = tmp_path / "s"
    first = SessionManager(root=root, workspace=ReleaseRecordingWorkspace(), local_source=Local())
    sid = await first.create()
    await first.load(sid, LocalRef(path=str(repo), branch="x", base="main"))
    await first.shutdown()

    workspace = ReleaseRecordingWorkspace()
    second = SessionManager(root=root, workspace=workspace, local_source=Local())
    await second.restore_all()
    await second.end(sid)
    assert workspace.released == []              # their clone is still theirs
    assert repo.exists()
    await second.shutdown()
