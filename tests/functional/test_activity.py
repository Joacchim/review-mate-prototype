"""Functional tests for the activity-channel — the per-actor republisher and GET /api/activity."""
import asyncio

from starlette.testclient import TestClient

from review_mate.activity.broker import ActivityBroker
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight, PostMessage
from review_mate.session.state import Side, LineRange, Origin

HL = dict(file="a.py", side=Side.NEW, line_range=LineRange(start=1, end=1))
HL_CMD = {"type": "add_highlight", "file": "a.py", "side": "new",
          "line_range": {"start": 1, "end": 1}}


async def test_republisher_emits_highlight_added(tmp_path):
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "highlight_added" and event.session_id == sid
    await mgr.shutdown()


async def test_republisher_emits_message_posted(tmp_path):
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    sid = await mgr.create()
    await mgr.get(sid).submit(PostMessage(body="hi"), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "message_posted" and event.session_id == sid
    await mgr.shutdown()


async def test_session_created_after_a_watcher_is_covered(tmp_path):
    broker = ActivityBroker()
    mgr = SessionManager(root=tmp_path / "s", activity_broker=broker)
    waiting = asyncio.create_task(broker.wait(since=0, timeout=1))
    await asyncio.sleep(0.01)                      # watcher parks before the session exists
    sid = await mgr.create()
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.BROWSER)
    event = await waiting
    assert event is not None and event.session_id == sid and event.kind == "highlight_added"
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
    # a reviewer highlight after the agent writes still gets through (seq advances normally)
    await mgr.get(sid).submit(AddHighlight(**HL), Origin.BROWSER)
    event = await broker.wait(since=0, timeout=1)
    assert event is not None and event.kind == "highlight_added" and event.session_id == sid
    await mgr.shutdown()


# --- GET /api/activity route (end-to-end via the app) ---

def test_route_returns_a_highlight_event(tmp_path):
    app = create_app(manager=SessionManager(root=tmp_path / "s"), with_mcp=False)
    with TestClient(app) as client:
        sid = client.post("/api/sessions", json={}).json()["id"]
        assert client.post(f"/api/sessions/{sid}/commands", json=HL_CMD).status_code == 200
        event = client.get("/api/activity?since=0").json()
        assert event["kind"] == "highlight_added" and event["session_id"] == sid
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
