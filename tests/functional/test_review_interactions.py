"""Functional tests for the review-interactions host/data plane (D19): submit-with-approve and the
thread verbs (reply / resolve / refresh) re-mirroring host truth into session state."""
import httpx

from review_mate.host.base import CapabilityError, GITLAB_CAPABILITIES
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.commands import ApplyMRMetadata, SaveDraft
from review_mate.session.state import MRMetadata, Origin, ReviewThread, ThreadComment
from review_mate.seams import MRRef
from review_mate.writeback.service import Writeback

MR = MRMetadata(host="gitlab", project="g/p", iid=42, title="T", source_branch="x",
                target_branch="main", sha="s", author="a", url="https://gl/g/p/-/merge_requests/42")


class StubWriter:
    def __init__(self, caps=None, fail=None):
        self.calls = []
        self._caps = dict(caps if caps is not None else GITLAB_CAPABILITIES)
        self._fail = fail  # a capability name to raise CapabilityError for

    def capabilities(self):
        return dict(self._caps)

    def _guard(self, cap):
        if self._fail == cap:
            raise CapabilityError(cap)

    async def post_comment(self, ref, position, body):
        self.calls.append(("post_comment", body)); return {"id": 1, "notes": [{"id": 1}]}

    async def post_mr_comment(self, ref, body):
        self.calls.append(("post_mr_comment", body)); return {"id": 2}

    async def reply(self, ref, tid, body):
        self._guard("threads"); self.calls.append(("reply", tid, body)); return {"id": 3}

    async def resolve(self, ref, tid, resolved=True):
        self._guard("threads"); self.calls.append(("resolve", tid, resolved)); return {}

    async def approve(self, ref):
        self._guard("approvals"); self.calls.append(("approve",)); return {}


class StubProvider:
    """A host provider whose fetch_threads returns whatever the test stages as 'current host truth'."""
    def __init__(self, threads=None):
        self.threads = threads or []

    async def fetch_threads(self, ref):
        return list(self.threads)


async def _app_client(tmp_path, writer, provider):
    manager = SessionManager(root=tmp_path / "s")
    app = create_app(manager=manager, with_mcp=False, provider=provider,
                     writeback=Writeback(manager, writer))
    sid = await manager.create()
    await manager.get(sid).submit(ApplyMRMetadata(mr=MR), Origin.SYSTEM)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://t")
    return manager, sid, client


async def test_submit_without_approve_posts_drafts_only(tmp_path):
    writer = StubWriter()
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider())
    async with client:
        await manager.get(sid).submit(SaveDraft(highlight_id=None, body="MR summary"), Origin.BROWSER)
        r = await client.post(f"/api/sessions/{sid}/submit-review", json={})
        data = r.json()
    assert data["approved"] is False and data["posted"] == 1
    assert ("approve",) not in writer.calls
    await manager.shutdown()


async def test_submit_with_approve_posts_then_approves(tmp_path):
    writer = StubWriter()
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider())
    async with client:
        await manager.get(sid).submit(SaveDraft(highlight_id=None, body="looks good"), Origin.BROWSER)
        r = await client.post(f"/api/sessions/{sid}/submit-review", json={"approve": True})
        data = r.json()
    assert data["approved"] is True and data["posted"] == 1
    assert writer.calls[-1] == ("approve",)
    await manager.shutdown()


async def test_approve_only_with_no_drafts(tmp_path):
    writer = StubWriter()
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider())
    async with client:
        r = await client.post(f"/api/sessions/{sid}/submit-review", json={"approve": True})
        data = r.json()
    assert data["approved"] is True and data["posted"] == 0 and data["total"] == 0
    assert writer.calls == [("approve",)]
    await manager.shutdown()


async def test_reply_posts_and_remirrors(tmp_path):
    # after the reply, host truth (StubProvider) carries the new note; the route must re-mirror it
    after = [ReviewThread(id="disc1", comments=[ThreadComment(id="1", author="rev", body="nit"),
                                                ThreadComment(id="2", author="me", body="fixed")])]
    writer = StubWriter()
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider(threads=after))
    async with client:
        r = await client.post(f"/api/sessions/{sid}/threads/disc1/reply", json={"body": "fixed"})
        assert r.json()["ok"] is True
    assert writer.calls[-1] == ("reply", "disc1", "fixed")
    threads = manager.get(sid).snapshot().threads
    assert len(threads) == 1 and [c.body for c in threads[0].comments] == ["nit", "fixed"]
    await manager.shutdown()


async def test_resolve_remirrors_resolved_state(tmp_path):
    after = [ReviewThread(id="disc1", comments=[ThreadComment(id="1", author="rev", body="nit")],
                          resolved=True)]
    writer = StubWriter()
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider(threads=after))
    async with client:
        r = await client.post(f"/api/sessions/{sid}/threads/disc1/resolve", json={"resolved": True})
        assert r.json() == {"ok": True, "resolved": True}
    assert writer.calls[-1] == ("resolve", "disc1", True)
    assert manager.get(sid).snapshot().threads[0].resolved is True
    await manager.shutdown()


async def test_refresh_pulls_threads_into_state(tmp_path):
    fresh = [ReviewThread(id="d9", comments=[ThreadComment(id="9", author="author", body="new")])]
    manager, sid, client = await _app_client(tmp_path, StubWriter(), StubProvider(threads=fresh))
    async with client:
        r = await client.post(f"/api/sessions/{sid}/refresh-threads", json={})
        assert r.json() == {"threads": 1}
    assert [t.id for t in manager.get(sid).snapshot().threads] == ["d9"]
    await manager.shutdown()


async def test_reply_capability_missing_returns_400(tmp_path):
    writer = StubWriter(fail="threads")
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider())
    async with client:
        r = await client.post(f"/api/sessions/{sid}/threads/disc1/reply", json={"body": "x"})
    assert r.status_code == 400
    assert manager.get(sid).snapshot().threads == []   # nothing mirrored on failure
    await manager.shutdown()
