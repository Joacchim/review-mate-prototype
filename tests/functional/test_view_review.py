"""The `review` scope: what the reviewer has prepared, and what it would take to send it.

Three facts a reviewer reads as one bar — the drafts, whether the change has moved past the version
they reviewed, and whether they have approved — were three separate reads before this. Folding them
into one scope is only worth it if the fold is the server's, so most of what is pinned here is the
shape of the answer rather than the plumbing that delivers it.

The invariant the rest rests on: `build` never calls the host. Approval is the one host fact here,
and it arrives through `refresh`, so a scope rebuilt on every draft keystroke costs nothing remote.
"""
import pytest

from conftest import HostStub
from review_mate.seams import MRRef
from review_mate.session.commands import (
    AddHighlight, MarkDraftPosted, RemoveDraft, SaveDraft,
)
from review_mate.session.manager import SessionManager
from review_mate.session.state import LineRange, Origin, Side
from review_mate.view.review import ReviewScope


class ApprovingHost(HostStub):
    """A host whose MRs can be approved, which counts how often it is asked, and can start failing."""

    def __init__(self, approved_by=(), **kwargs):
        super().__init__(**kwargs)
        self.approval_calls = 0
        self.fail = False
        self._approved_by = list(approved_by)

    async def load(self, ref: MRRef):
        payload = await super().load(ref)
        payload.mr.capabilities = {"approvals": True}
        return payload

    async def approvals(self, ref: MRRef) -> dict:
        self.approval_calls += 1
        if self.fail:
            raise RuntimeError("host is down")
        return {"approved_by": list(self._approved_by),
                "you_approved": self.username in self._approved_by}


class Watermarks:
    """The slice of the KB this scope reads."""

    def __init__(self, mark=None):
        self.mark = mark

    def get_watermark(self, host, project, iid):
        return self.mark


@pytest.fixture
async def review(tmp_path):
    async def build(host=None, kb=None):
        manager = SessionManager(root=tmp_path / "sessions", mr_source=host or HostStub())
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        return manager, sid, ReviewScope(manager, provider=host, kb=kb)
    yield build


async def mark(manager, sid, line=1):
    """A highlight to hang a draft on — a draft's anchor has to exist for it to be reachable."""
    await manager.get(sid).submit(
        AddHighlight(file="a.py", side=Side.NEW, line_range=LineRange(start=line, end=line)),
        Origin.BROWSER)
    return manager.get(sid).snapshot().highlights[-1].id


async def save(manager, sid, body, highlight_id=None):
    await manager.get(sid).submit(SaveDraft(highlight_id=highlight_id, body=body), Origin.BROWSER)


# --- what the reviewer has written ------------------------------------------

async def test_a_review_with_nothing_prepared_says_so(review):
    manager, sid, scope = await review()
    view = await scope.build(sid)
    assert view["state"] == "ready"
    assert view["drafts"] == [] and view["pending"] == 0 and view["posted"] == 0
    await manager.shutdown()


async def test_a_draft_carries_its_text_and_its_anchor(review):
    manager, sid, scope = await review()
    hid = await mark(manager, sid)
    await save(manager, sid, "this needs a test", highlight_id=hid)
    await save(manager, sid, "reads well overall")          # MR-level: anchored to nothing
    view = await scope.build(sid)

    anchored = [d for d in view["drafts"] if d["highlight_id"] == hid]
    summary = [d for d in view["drafts"] if d["highlight_id"] is None]
    assert len(anchored) == 1 and anchored[0]["body"] == "this needs a test"
    assert len(summary) == 1 and summary[0]["body"] == "reads well overall"
    assert view["pending"] == 2 and view["posted"] == 0
    await manager.shutdown()


async def test_posting_a_draft_moves_it_between_the_counts(review):
    manager, sid, scope = await review()
    hid = await mark(manager, sid)
    await save(manager, sid, "this needs a test", highlight_id=hid)
    await manager.get(sid).submit(
        MarkDraftPosted(highlight_id=hid, url="http://note/1", thread_id="t9"), Origin.BROWSER)
    view = await scope.build(sid)

    posted = view["drafts"][0]
    assert posted["status"] == "posted"
    assert posted["url"] == "http://note/1" and posted["thread_id"] == "t9"
    assert view["pending"] == 0 and view["posted"] == 1
    await manager.shutdown()


async def test_a_discarded_draft_leaves_nothing_behind(review):
    manager, sid, scope = await review()
    hid = await mark(manager, sid)
    await save(manager, sid, "never mind", highlight_id=hid)
    await manager.get(sid).submit(RemoveDraft(highlight_id=hid), Origin.BROWSER)
    view = await scope.build(sid)
    assert view["drafts"] == [] and view["pending"] == 0
    await manager.shutdown()


# --- whether they have approved ---------------------------------------------

async def test_an_mr_that_cannot_be_approved_is_answered_not_unknown(review):
    """`available: false` is a settled answer. A client must not render it as "still asking"."""
    manager, sid, scope = await review()          # the plain stub advertises no approvals
    approval = (await scope.build(sid))["approval"]
    assert approval["available"] is False and approval["checked"] is True
    await manager.shutdown()


async def test_an_approvable_mr_is_unknown_until_someone_asks(review):
    host = ApprovingHost()
    manager, sid, scope = await review(host=host)

    before = (await scope.build(sid))["approval"]
    assert before["available"] is True and before["checked"] is False
    assert host.approval_calls == 0, "building must not reach the host"

    await scope.refresh(sid)
    after = (await scope.build(sid))["approval"]
    assert after["checked"] is True and after["you_approved"] is False
    assert host.approval_calls == 1
    await manager.shutdown()


async def test_the_reviewers_own_approval_is_named(review):
    host = ApprovingHost(approved_by=["reviewer", "someone-else"])
    manager, sid, scope = await review(host=host)
    await scope.refresh(sid)
    approval = (await scope.build(sid))["approval"]
    assert approval["you_approved"] is True
    assert approval["approved_by"] == ["reviewer", "someone-else"]
    await manager.shutdown()


async def test_a_failed_refresh_leaves_the_last_answer_standing(review):
    """A bar showing "you approved" must not blink to unknown because one refresh did not land."""
    host = ApprovingHost(approved_by=["reviewer"])
    manager, sid, scope = await review(host=host)
    await scope.refresh(sid)
    assert (await scope.build(sid))["approval"]["you_approved"] is True

    host.fail = True
    with pytest.raises(RuntimeError):      # it propagates; the caller decides what to say
        await scope.refresh(sid)
    assert (await scope.build(sid))["approval"]["you_approved"] is True
    await manager.shutdown()


# --- whether the change has moved past them ---------------------------------

async def test_a_review_at_the_head_is_not_behind(review):
    manager, sid, scope = await review(kb=Watermarks(mark="abc"))     # the stub's head is `abc`
    version = (await scope.build(sid))["version"]
    assert version["head"] == "abc" and version["watermark"] == "abc"
    assert version["behind"] is False
    await manager.shutdown()


async def test_a_change_that_moved_past_the_watermark_reads_behind(review):
    manager, sid, scope = await review(kb=Watermarks(mark="older"))
    version = (await scope.build(sid))["version"]
    assert version["behind"] is True and version["watermark"] == "older"
    await manager.shutdown()


async def test_a_review_never_marked_has_no_watermark(review):
    manager, sid, scope = await review(kb=Watermarks(mark=None))
    version = (await scope.build(sid))["version"]
    assert version["watermark"] is None and version["behind"] is False
    await manager.shutdown()


# --- the edges ---------------------------------------------------------------

async def test_a_session_that_is_not_there_says_unknown_session(review):
    manager, _sid, scope = await review()
    assert (await scope.build("no-such-session"))["state"] == "unknown-session"
    await manager.shutdown()


async def test_building_never_reaches_the_host(review):
    """The scope rebuilds on every draft keystroke, so a host call here would be a call per letter."""
    host = ApprovingHost()
    manager, sid, scope = await review(host=host)
    before = host.calls["summary"] + host.approval_calls
    for n in range(5):
        await save(manager, sid, f"draft {n}", highlight_id=await mark(manager, sid, line=n + 1))
        await scope.build(sid)
    assert host.calls["summary"] + host.approval_calls == before
    await manager.shutdown()
