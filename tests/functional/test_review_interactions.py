"""Functional tests for the review-interactions host/data plane (D19): submit-with-approve and the
thread verbs (reply / resolve / refresh) re-mirroring host truth into session state."""
import httpx

from review_mate.host.base import CapabilityError, GITLAB_CAPABILITIES
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight, ApplyMRMetadata, SaveDraft
from review_mate.session.state import LineRange, MRMetadata, Origin, ReviewThread, Side, ThreadComment
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
        self.calls.append(("post_comment", body)); return {"id": "disc-new", "notes": [{"id": 1}]}

    async def post_mr_comment(self, ref, body):
        self.calls.append(("post_mr_comment", body)); return {"id": 2}

    async def reply(self, ref, tid, body):
        self._guard("threads"); self.calls.append(("reply", tid, body)); return {"id": 3}

    async def resolve(self, ref, tid, resolved=True):
        self._guard("threads"); self.calls.append(("resolve", tid, resolved)); return {}

    async def approve(self, ref):
        self._guard("approvals"); self.calls.append(("approve",)); return {}

    async def edit_note(self, ref, tid, nid, body):
        self._guard("threads"); self.calls.append(("edit_note", tid, nid, body)); return {}

    async def delete_note(self, ref, tid, nid):
        self._guard("threads"); self.calls.append(("delete_note", tid, nid)); return {}


class StubProvider:
    """A host provider whose fetch_threads returns whatever the test stages as 'current host truth'."""
    def __init__(self, threads=None, blame=None, issues=None):
        self.threads = threads or []
        self._blame = blame or []
        self._issues = issues or []

    async def fetch_threads(self, ref):
        return list(self.threads)

    async def blame(self, project, path, ref, start, end):
        return list(self._blame)

    async def linked_issues(self, project, iid):
        return list(self._issues)


async def _app_client(tmp_path, writer, provider):
    from review_mate.kb.store import ReviewKB
    manager = SessionManager(root=tmp_path / "s")
    kb = ReviewKB(root=tmp_path / "kb")            # tmp-rooted — never touch the real ~/.review-mate
    app = create_app(manager=manager, with_mcp=False, provider=provider,
                     writeback=Writeback(manager, writer), kb=kb)
    sid = await manager.create()
    await manager.get(sid).submit(ApplyMRMetadata(mr=MR), Origin.SYSTEM)
    manager._test_kb = kb                          # expose to tests
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


async def test_context_returns_cheap_tier(tmp_path):
    blame = [{"lines": [5, 5], "commit": "abc123", "author": "dev", "date": "d", "summary": "guard"}]
    issues = [{"iid": 7, "title": "Fix leak", "url": "u"}]
    provider = StubProvider(blame=blame, issues=issues)
    manager, sid, client = await _app_client(tmp_path, StubWriter(), provider)
    async with client:
        r = await client.get(f"/api/sessions/{sid}/context?file=a.py&start=5&end=5")
        data = r.json()
    assert data["blame"] == blame and data["linked_issues"] == issues
    await manager.shutdown()


async def test_context_degrades_without_provider(tmp_path):
    # no provider → the cheap tier is empty, not an error (agent plane may still be absent too)
    manager = SessionManager(root=tmp_path / "s")
    app = create_app(manager=manager, with_mcp=False)  # no provider, no writeback
    sid = await manager.create()
    await manager.get(sid).submit(ApplyMRMetadata(mr=MR), Origin.SYSTEM)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get(f"/api/sessions/{sid}/context?file=a.py&start=1&end=1")
        assert r.json() == {"blame": [], "linked_issues": []}
    await manager.shutdown()


async def test_submit_composes_suggestion_and_captures_thread_id(tmp_path):
    writer = StubWriter()
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider())
    async with client:
        actor = manager.get(sid)
        await actor.submit(AddHighlight(file="a.py", side=Side.NEW,
                                        line_range=LineRange(start=5, end=6)), Origin.BROWSER)
        hid = actor.snapshot().highlights[0].id
        await actor.submit(SaveDraft(highlight_id=hid, body="prefer a guard",
                                     suggestion="if x is not None:"), Origin.BROWSER)
        r = await client.post(f"/api/sessions/{sid}/submit-review", json={})
        assert r.json()["posted"] == 1
    # the posted body carries prose + a fenced suggestion block spanning the highlight (span=1)
    kind, body = writer.calls[-1]
    assert kind == "post_comment"
    assert "prefer a guard" in body and "```suggestion:-0+1" in body and "if x is not None:" in body
    # the draft is linked to the discussion it became
    assert manager.get(sid).snapshot().drafts[0].thread_id == "disc-new"
    await manager.shutdown()


async def test_edit_and_delete_note_routes(tmp_path):
    writer = StubWriter()
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider())
    async with client:
        await client.post(f"/api/sessions/{sid}/threads/disc1/notes/7/edit", json={"body": "reworded"})
        assert writer.calls[-1] == ("edit_note", "disc1", "7", "reworded")
        await client.post(f"/api/sessions/{sid}/threads/disc1/notes/7/delete", json={})
        assert writer.calls[-1] == ("delete_note", "disc1", "7")
    await manager.shutdown()


async def test_whoami_returns_reviewer_username(tmp_path):
    class NamedProvider(StubProvider):
        username = "reviewer-joe"
    manager, sid, client = await _app_client(tmp_path, StubWriter(), NamedProvider())
    async with client:
        assert (await client.get("/api/me")).json() == {"username": "reviewer-joe"}
    await manager.shutdown()


async def test_review_status_and_mark_reviewed(tmp_path):
    manager, sid, client = await _app_client(tmp_path, StubWriter(), StubProvider())
    kb = manager._test_kb
    async with client:
        assert (await client.get(f"/api/sessions/{sid}/review-status")).json()["behind"] is False
        kb.set_watermark(MR.host, MR.project, MR.iid, "old-sha")   # a prior review at an older head
        st = (await client.get(f"/api/sessions/{sid}/review-status")).json()
        assert st["behind"] is True and st["watermark"] == "old-sha" and st["head"] == "s"
        assert (await client.post(f"/api/sessions/{sid}/mark-reviewed", json={})).json()["watermark"] == "s"
        assert kb.get_watermark(MR.host, MR.project, MR.iid) == "s"
        assert (await client.get(f"/api/sessions/{sid}/review-status")).json()["behind"] is False
    await manager.shutdown()


async def test_submit_advances_watermark(tmp_path):
    manager, sid, client = await _app_client(tmp_path, StubWriter(), StubProvider())
    kb = manager._test_kb
    async with client:
        kb.set_watermark(MR.host, MR.project, MR.iid, "old-sha")
        await client.post(f"/api/sessions/{sid}/submit-review", json={})
        assert kb.get_watermark(MR.host, MR.project, MR.iid) == "s"   # submitting advances it
    await manager.shutdown()


async def test_highlight_records_created_sha(tmp_path):
    manager, sid, client = await _app_client(tmp_path, StubWriter(), StubProvider())
    async with client:
        actor = manager.get(sid)
        await actor.submit(AddHighlight(file="a.py", side=Side.NEW,
                                        line_range=LineRange(start=1, end=1)), Origin.BROWSER)
        assert actor.snapshot().highlights[0].created_sha == "s"   # the MR head at creation
    await manager.shutdown()


async def test_reply_capability_missing_returns_400(tmp_path):
    writer = StubWriter(fail="threads")
    manager, sid, client = await _app_client(tmp_path, writer, StubProvider())
    async with client:
        r = await client.post(f"/api/sessions/{sid}/threads/disc1/reply", json={"body": "x"})
    assert r.status_code == 400
    assert manager.get(sid).snapshot().threads == []   # nothing mirrored on failure
    await manager.shutdown()
