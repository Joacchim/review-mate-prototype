"""Functional tests for the activity-channel — the per-actor republisher and GET /api/activity."""
import asyncio

from review_mate.activity.broker import ActivityBroker
from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight, PostMessage
from review_mate.session.state import Side, LineRange, Origin

HL = dict(file="a.py", side=Side.NEW, line_range=LineRange(start=1, end=1))


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
