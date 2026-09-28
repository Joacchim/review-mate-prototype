"""Functional tests for the activity-channel — the per-actor republisher and GET /api/activity."""
import asyncio

import httpx
from httpx import ASGITransport
from starlette.testclient import TestClient

from review_mate.activity.broker import ActivityBroker
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.commands import (
    AddHighlight, DecideAccess, EmitCard, PostMessage, RecordGrant, RequestAccess, RequestCheck,
    RequestContext, RequestInsights,
)
from review_mate.session.state import Grant, Side, LineRange, Origin, Subject, SubjectKind

HL = dict(file="a.py", side=Side.NEW, line_range=LineRange(start=1, end=1))
HL_CMD = {"type": "add_highlight", "file": "a.py", "side": "new",
          "line_range": {"start": 1, "end": 1}}


async def test_bare_highlight_does_not_republish(tmp_path):
    # D21: a bare highlight gets the host context and must NOT wake the agent
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.BROWSER)
    assert await broker.wait(since=0, timeout=0.2) is None
    await mgr.shutdown()


async def test_request_context_republishes(tmp_path):
    # escalating a highlight is what wakes the agent
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.BROWSER)
    hid = mgr.get(sid).snapshot().highlights[0].id
    await mgr.get(sid).submit(RequestContext(highlight_id=hid, question="why?"), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "context_requested" and event.session_id == sid
    await mgr.shutdown()


async def test_republisher_emits_message_posted(tmp_path):
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(PostMessage(body="hi"), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "message_posted" and event.session_id == sid
    await mgr.shutdown()


async def test_asking_for_a_pass_republishes(tmp_path):
    """The reviewer is waiting on it, and the coordinator does not re-sweep durable state in its
    steady-state loop — an ask nothing announces sits until the agent happens to restart."""
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(RequestInsights(), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "insights_requested" and event.session_id == sid
    await mgr.shutdown()


async def test_asking_for_a_double_check_republishes(tmp_path):
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.BROWSER)   # announces nothing on its own
    hid = mgr.get(sid).snapshot().highlights[0].id
    await mgr.get(sid).submit(
        RequestCheck(subject=Subject(kind=SubjectKind.HIGHLIGHT, id=hid)), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "check_requested" and event.session_id == sid
    await mgr.shutdown()


async def test_every_ask_the_reviewer_raises_is_announced(tmp_path):
    """`view.asks` and this stream have to cover the same set.

    An ask that is listed as outstanding and announced by nothing is a silence: the reviewer sees a
    waiting cue, and the agent is told to go and look by nothing, because the coordinator's
    steady-state loop reacts to this stream and re-derives durable state only after a restart.

    Behavioural, so it goes through the real republisher — but it cannot notice a *fifth* ask kind
    that forgets to announce itself. Nothing cheap can; the four are named here and in `view.asks`.
    """
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    actor = mgr.get(sid)
    await actor.submit(AddHighlight(**HL), Origin.BROWSER)
    hid = actor.snapshot().highlights[0].id
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=hid)
    await actor.submit(RequestContext(highlight_id=hid), Origin.BROWSER)
    await actor.submit(PostMessage(body="and this?", anchor=subject), Origin.BROWSER)
    await actor.submit(RequestInsights(), Origin.BROWSER)
    await actor.submit(RequestCheck(subject=subject), Origin.BROWSER)

    seen, since = [], 0
    while len(seen) < 4:
        event = await broker.wait(since=since, timeout=1)
        assert event is not None, f"only {[e.kind for e in seen]} announced"
        seen.append(event)
        since = event.seq
    assert [e.kind for e in seen] == ["context_requested", "message_posted",
                                      "insights_requested", "check_requested"]
    assert {e.session_id for e in seen} == {sid}
    await mgr.shutdown()


async def test_session_created_after_a_watcher_is_covered(tmp_path):
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    waiting = asyncio.create_task(broker.wait(since=0, timeout=1))
    await asyncio.sleep(0.01)                      # watcher parks before the session exists
    sid = await mgr.create()
    await mgr.get(sid).submit(PostMessage(body="hi"), Origin.BROWSER)
    event = await waiting
    assert event is not None and event.session_id == sid and event.kind == "message_posted"
    await mgr.shutdown()


async def test_no_broker_is_a_noop(tmp_path):
    mgr = SessionManager(root=tmp_path / "s")     # baseline: no broker configured
    sid = await mgr.create()
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.BROWSER)   # must not raise
    await mgr.shutdown()


async def test_agent_authored_writes_do_not_republish(tmp_path):
    # only the reviewer's actions wake the agent — a worker's own highlight/message (Origin.AGENT)
    # must NOT re-publish as activity, else it re-invokes the coordinator and re-wakes itself.
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.AGENT)
    await mgr.get(sid).submit(PostMessage(body="an agent reply"), Origin.AGENT)
    assert await broker.wait(since=0, timeout=0.2) is None   # nothing republished
    # a reviewer chat message still gets through (seq advances normally)
    await mgr.get(sid).submit(PostMessage(body="hi from the reviewer"), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "message_posted" and event.session_id == sid
    await mgr.shutdown()


# --- GET /api/activity route (end-to-end via the app) ---

def test_route_returns_a_context_requested_event(tmp_path):
    app = create_app(manager=SessionManager(root=tmp_path / "s"), with_mcp=False)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.post(f"/api/sessions/{sid}/commands", json=HL_CMD).status_code == 200
        hid = client.get(f"/api/sessions/{sid}").json()["highlights"][0]["id"]
        # the bare highlight woke no one; the explicit escalation is what surfaces on activity
        assert client.post(f"/api/sessions/{sid}/commands",
                           json={"type": "request_context", "highlight_id": hid}).status_code == 200
        event = client.get("/api/activity?since=0").json()
        assert event["kind"] == "context_requested" and event["session_id"] == sid
        assert event["seq"] >= 1


def test_route_returns_a_lookup_event_with_query(tmp_path):
    app = create_app(manager=SessionManager(root=tmp_path / "s"), with_mcp=False)
    with TestClient(app) as client:
        client.post("/api/lookup", json={"query": "the cache MR"})
        event = client.get("/api/activity?since=0").json()
        assert event["kind"] == "lookup_opened" and event["query"] == "the cache MR"
        assert event["lookup_id"]


def test_route_exposes_shared_broker_on_app_state(tmp_path):
    app = create_app(manager=SessionManager(root=tmp_path / "s"), with_mcp=False)
    assert isinstance(app.state.activity_broker, ActivityBroker)

# --- GET /api/outstanding — reconcile from durable state, the answer to a dropped notification ---

def test_outstanding_is_a_work_list_not_a_census(tmp_path):
    """A session with nothing owed must not appear: the agent reconciles by iterating this, so an
    idle session in the list is work it would re-do."""
    app = create_app(manager=SessionManager(root=tmp_path / "s"), with_mcp=False)
    with TestClient(app) as client:
        client.post("/api/sessions", json={})
        assert client.get("/api/outstanding").json() == {"sessions": [], "total": 0}


def test_outstanding_surfaces_a_trailing_user_message(tmp_path):
    """The exact case a restart strands: the reviewer asked, the notification died with the old
    process, and durable state is the only remaining trace."""
    app = create_app(manager=SessionManager(root=tmp_path / "s"), with_mcp=False)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.post(f"/api/sessions/{sid}/commands", json={"type": "post_message", "body": "look?"})
        data = client.get("/api/outstanding").json()
        assert data["total"] == 1
        assert [s["session_id"] for s in data["sessions"]] == [sid]
        assert data["sessions"][0]["asks"][0]["kind"] == "chat"
        assert data["sessions"][0]["asks"][0]["since"]


def test_outstanding_ignores_a_bare_highlight_but_counts_an_escalation(tmp_path):
    """D21: a bare highlight is served by the host context and owes the agent nothing; only an explicit
    request_context is an ask. One predicate serves this route and the chat scope, so the agent and
    the reviewer cannot disagree about what is outstanding."""
    app = create_app(manager=SessionManager(root=tmp_path / "s"), with_mcp=False)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        client.post(f"/api/sessions/{sid}/commands", json=HL_CMD)
        assert client.get("/api/outstanding").json()["total"] == 0
        hid = client.get(f"/api/sessions/{sid}").json()["highlights"][0]["id"]
        client.post(f"/api/sessions/{sid}/commands",
                    json={"type": "request_context", "highlight_id": hid})
        ask = client.get("/api/outstanding").json()["sessions"][0]["asks"][0]
        assert ask["kind"] == "context" and ask["highlight_id"] == hid and ask["file"] == "a.py"


async def test_outstanding_clears_once_the_agent_answers(tmp_path):
    """Both ask kinds are satisfiable, and the agent's own work is what closes them — otherwise a
    reconcile sweep would loop on work it has already done."""
    mgr = SessionManager(root=tmp_path / "s")
    app = create_app(manager=mgr, with_mcp=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        sid = (await c.post("/api/sessions", json={})).json()["id"]
        actor = mgr.get(sid)
        await actor.submit(PostMessage(body="look?"), Origin.BROWSER)
        await actor.submit(AddHighlight(**HL), Origin.BROWSER)
        hid = actor.snapshot().highlights[0].id
        await actor.submit(RequestContext(highlight_id=hid), Origin.BROWSER)
        assert (await c.get("/api/outstanding")).json()["total"] == 2

        await actor.submit(EmitCard(highlight_id=hid, body="here"), Origin.AGENT)
        assert [a["kind"] for a in (await c.get("/api/outstanding")).json()["sessions"][0]["asks"]] \
            == ["chat"]
        await actor.submit(PostMessage(body="had a look"), Origin.AGENT)   # trailing role → agent
        assert (await c.get("/api/outstanding")).json() == {"sessions": [], "total": 0}
    await mgr.shutdown()


# --- consent, once the agent can act on it ------------------------------------

async def _asked_and_decided(mgr, sid, approve):
    actor = mgr.get(sid)
    await actor.submit(RequestAccess(repo="g/sibling", reason="the contract"), Origin.AGENT)
    rid = actor.snapshot().access_requests[-1].id
    await actor.submit(DecideAccess(request_id=rid, approve=approve), Origin.BROWSER)
    return actor, rid


async def test_asking_for_access_does_not_wake_the_agent(tmp_path):
    """It is the agent's own move. Waking it for its own write is the loop this filter exists for."""
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(RequestAccess(repo="g/x", reason="r"), Origin.AGENT)
    assert await broker.wait(since=0, timeout=0.2) is None
    await mgr.shutdown()


async def test_an_approval_alone_does_not_wake_the_agent(tmp_path):
    """There is nothing to act on yet — the repository is still being cloned and has no path."""
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await _asked_and_decided(mgr, sid, approve=True)
    assert await broker.wait(since=0, timeout=0.2) is None
    await mgr.shutdown()


async def test_a_host_resync_does_not_wake_the_agent(tmp_path):
    """The filter admits SYSTEM writes so a grant can wake the agent — and admitting the origin is
    not admitting everything on it. Host reconciliation is the server's bookkeeping, not work."""
    from review_mate.session.commands import ApplyFiles, ApplyMRMetadata
    from review_mate.session.state import ChangeType, FileEntry, MRMetadata
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(ApplyMRMetadata(mr=MRMetadata(
        host="gitlab", project="g/p", iid=1, title="t", source_branch="x", target_branch="main",
        sha="abc", author="d", url="u")), Origin.SYSTEM)
    await mgr.get(sid).submit(ApplyFiles(files=[FileEntry(
        path="a.py", change_type=ChangeType.MODIFIED, hunks=[])]), Origin.SYSTEM)
    assert await broker.wait(since=0, timeout=0.2) is None
    await mgr.shutdown()


async def test_a_refusal_wakes_the_agent(tmp_path):
    """A no is actionable: stop waiting on it and say what could not be checked."""
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await _asked_and_decided(mgr, sid, approve=False)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "access_settled" and event.session_id == sid
    await mgr.shutdown()


async def test_the_repository_landing_wakes_the_agent(tmp_path):
    """The case that was silent: a worker asked, timed out, parked, and the clone then finished."""
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    actor, rid = await _asked_and_decided(mgr, sid, approve=True)
    await actor.submit(RecordGrant(request_id=rid, grant=Grant(state="materializing")),
                       Origin.SYSTEM)
    assert await broker.wait(since=0, timeout=0.2) is None, "a clone starting is not yet news"

    await actor.submit(RecordGrant(request_id=rid, grant=Grant(state="ready", path="/tmp/x")),
                       Origin.SYSTEM)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "access_settled"
    await mgr.shutdown()


async def test_a_grant_that_failed_wakes_the_agent_too(tmp_path):
    """Otherwise it waits forever on a repository that is never coming."""
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    actor, rid = await _asked_and_decided(mgr, sid, approve=True)
    await actor.submit(RecordGrant(request_id=rid, grant=Grant(state="failed", error="no repo")),
                       Origin.SYSTEM)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "access_settled"
    await mgr.shutdown()
