"""Functional tests for the agent bridge + MCP server wiring."""
import asyncio
import json

import pytest

from review_mate.mcp.bridge import AgentBridge
from review_mate.mcp.server import build_mcp_server
from review_mate.session.manager import SessionManager
from review_mate.session.commands import (
    AddHighlight, DecideAccess, RecordGrant, SaveDraft,
)
from review_mate.session.state import Grant, LineRange, Origin, Side


@pytest.fixture
async def setup(tmp_path):
    from review_mate.view.access import AccessTopic
    from review_mate.view.agent import AgentView
    from review_mate.view.chat import ChatTopics
    from review_mate.view.annotations import AnnotationsTopic
    from review_mate.view.threads import ThreadsTopic
    manager = SessionManager(root=tmp_path / "sessions")
    view = AgentView(manager, annotations=AnnotationsTopic(manager), chat=ChatTopics(manager),
                     threads=ThreadsTopic(manager), access=AccessTopic(manager))
    bridge = AgentBridge(manager, view=view)
    sid = await manager.create()
    yield manager, bridge, sid
    await manager.shutdown()


async def _add_highlight(manager, sid, file="a.py"):
    writer = manager.get(sid)
    res = await writer.submit(
        AddHighlight(file=file, side=Side.NEW, line_range=LineRange(start=1, end=1)),
        Origin.BROWSER,
    )
    return res


async def test_emit_card_for_highlight(setup):  # AC-1
    manager, bridge, sid = setup
    await _add_highlight(manager, sid)
    hl_id = bridge.snapshot(sid).highlights[0].id
    res = await bridge.emit_card(sid, highlight_id=hl_id, body="context here")
    assert res.ok
    assert bridge.snapshot(sid).cards[0].body == "context here"
    assert bridge.snapshot(sid).cards[0].author is Origin.AGENT


async def test_emit_card_unknown_highlight_not_ok(setup):  # AC-2 (core rejects)
    manager, bridge, sid = setup
    res = await bridge.emit_card(sid, highlight_id="nope", body="x")
    assert not res.ok


async def test_mr_level_card_has_no_anchor(setup):
    manager, bridge, sid = setup
    res = await bridge.emit_card(sid, highlight_id=None, body="MR-level insight")
    assert res.ok
    card = bridge.snapshot(sid).cards[0]
    assert card.highlight_id is None and card.author is Origin.AGENT


async def test_add_insight_creates_agent_highlight_and_card(setup):
    manager, bridge, sid = setup
    out = await bridge.add_insight(sid, file="m.py", start=40, end=42, body="this can NPE")
    snap = bridge.snapshot(sid)
    hl = snap.highlights[0]
    assert hl.author is Origin.AGENT and hl.file == "m.py" and hl.line_range.start == 40
    assert out["highlight_id"] == hl.id
    card = next(c for c in snap.cards if c.highlight_id == hl.id)
    assert card.body == "this can NPE"


async def test_add_insight_tool_registered(setup):
    manager, bridge, sid = setup
    server = build_mcp_server(bridge)
    names = {t.name for t in await server.list_tools()}
    assert "add_insight" in names


async def test_wait_for_highlight_resolves(setup):  # AC-3
    manager, bridge, sid = setup
    waiter = asyncio.create_task(bridge.wait_for_highlight(sid, since=bridge.snapshot(sid).seq))
    await asyncio.sleep(0)  # let the waiter subscribe
    await _add_highlight(manager, sid, file="watched.py")
    got = await asyncio.wait_for(waiter, 1)
    assert got is not None
    assert got["highlight"].file == "watched.py"
    assert got["seq"] >= 1


async def test_request_access_records_pending(setup):  # AC-4
    manager, bridge, sid = setup
    res = await bridge.request_access(sid, repo="g/other", reason="contract")
    assert res.ok
    reqs = bridge.snapshot(sid).access_requests
    assert reqs[0].repo == "g/other" and reqs[0].status.value == "pending"


def test_snapshot_unknown_raises(setup):  # AC-5
    manager, bridge, sid = setup
    with pytest.raises(KeyError):
        bridge.snapshot("nope")


async def test_mcp_server_registers_tools(setup):  # AC-6
    manager, bridge, sid = setup
    server = build_mcp_server(bridge)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    for expected in ("list_sessions", "get_session", "wait_for_highlight",
                     "emit_card", "request_access", "access_state", "wait_for_access"):
        assert expected in names


# --- the agent's half of consent ----------------------------------------------

async def _decide(manager, sid, rid, approve):
    await manager.get(sid).submit(DecideAccess(request_id=rid, approve=approve), Origin.BROWSER)


async def test_the_agent_can_read_what_it_was_refused(setup):
    """A refusal it cannot see is a refusal it will ask about again."""
    manager, bridge, sid = setup
    await bridge.request_access(sid, repo="g/other", reason="contract")
    rid = bridge.snapshot(sid).access_requests[0].id
    await _decide(manager, sid, rid, approve=False)
    row = (await bridge.access_state(sid))[0]
    assert row["status"] == "denied" and row["state"] is None and row["path"] is None


async def test_the_agent_reads_the_path_only_once_it_is_ready(setup):
    manager, bridge, sid = setup
    await bridge.request_access(sid, repo="g/other", reason="contract")
    rid = bridge.snapshot(sid).access_requests[0].id
    await _decide(manager, sid, rid, approve=True)
    assert (await bridge.access_state(sid))[0] == {
        "id": rid, "repo": "g/other", "reason": "contract", "status": "approved",
        "state": None, "path": None, "error": ""}, "approved is not yet readable"

    await manager.get(sid).submit(
        RecordGrant(request_id=rid, grant=Grant(state="materializing")), Origin.SYSTEM)
    assert (await bridge.access_state(sid))[0]["state"] == "materializing"
    assert (await bridge.access_state(sid))[0]["path"] is None

    await manager.get(sid).submit(
        RecordGrant(request_id=rid, grant=Grant(state="ready", path="/tmp/x")), Origin.SYSTEM)
    row = (await bridge.access_state(sid))[0]
    assert row["state"] == "ready" and row["path"] == "/tmp/x"


async def test_waiting_on_consent_returns_on_a_refusal_too(setup):
    """Blocking until an approval that never comes is how an agent stops with nothing said."""
    manager, bridge, sid = setup
    await bridge.request_access(sid, repo="g/other", reason="contract")
    rid = bridge.snapshot(sid).access_requests[0].id
    waiting = asyncio.create_task(bridge.wait_for_access(sid, since=0, timeout=2))
    await asyncio.sleep(0)
    await _decide(manager, sid, rid, approve=False)
    answer = await waiting
    assert answer is not None and answer["status"] == "denied"


async def test_waiting_on_consent_returns_when_the_repository_lands(setup):
    manager, bridge, sid = setup
    await bridge.request_access(sid, repo="g/other", reason="contract")
    rid = bridge.snapshot(sid).access_requests[0].id
    await _decide(manager, sid, rid, approve=True)
    since = manager.get(sid).snapshot().seq
    waiting = asyncio.create_task(bridge.wait_for_access(sid, since=since, timeout=2))
    await asyncio.sleep(0)
    await manager.get(sid).submit(
        RecordGrant(request_id=rid, grant=Grant(state="ready", path="/tmp/x")), Origin.SYSTEM)
    answer = await waiting
    assert answer is not None and answer["state"] == "ready" and answer["path"] == "/tmp/x"


async def test_waiting_times_out_rather_than_hanging(setup):
    manager, bridge, sid = setup
    await bridge.request_access(sid, repo="g/other", reason="contract")
    assert await bridge.wait_for_access(sid, since=0, timeout=0.1) is None


async def test_the_real_app_gives_the_agent_the_folded_view(tmp_path):
    """A bridge built by hand proves nothing about the one the server builds — that is how the
    cross-repo broker stayed unwired for its whole life."""
    from review_mate.server.app import create_app
    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager, with_mcp=True)
    async with app.router.lifespan_context(app):
        sid = await manager.create()
        writer = manager.get(sid)
        await writer.submit(AddHighlight(file="a.py", side=Side.NEW,
                                        line_range=LineRange(start=1, end=1)), Origin.BROWSER)
        hid = writer.snapshot().highlights[0].id
        await writer.submit(SaveDraft(highlight_id=hid, body="a candid unsent note"), Origin.BROWSER)

        built = await app.state.bridge.view(sid)
        assert "annotations" in built and "chat" in built and "access" in built
        assert "a candid unsent note" not in repr(built)
        assert "files" not in built


async def test_the_tool_the_agent_actually_calls_returns_the_folded_view(setup):
    """Through `call_tool`, not the bridge beneath it: the tool layer is the agent's real contract,
    and a bridge test passes whether or not the tool is wired to it."""
    manager, bridge, sid = setup
    writer = manager.get(sid)
    await writer.submit(AddHighlight(file="a.py", side=Side.NEW,
                                    line_range=LineRange(start=1, end=1)), Origin.BROWSER)
    hid = writer.snapshot().highlights[0].id
    await writer.submit(SaveDraft(highlight_id=hid, body="a candid unsent note"), Origin.BROWSER)

    server = build_mcp_server(bridge)
    returned = await server.call_tool("get_session", {"session_id": sid})
    # what the agent literally receives: the serialized tool result, not the object behind it
    text = "".join(part.text for part in (returned[0] if isinstance(returned, tuple) else returned))
    payload = json.loads(text)

    assert "annotations" in payload and "chat" in payload
    assert "asks" in payload["chat"]["agent"], "the backlog, so the worker stops deriving it"
    assert "a candid unsent note" not in repr(payload)
    assert "files" not in payload, "the diff has its own tool"


# --- classifying what it found -------------------------------------------------

async def test_the_agent_classifies_what_it_posts(setup):
    manager, bridge, sid = setup
    await bridge.emit_card(sid, None, "the retry is unbounded", theme="bug", criticality="high",
                           about="the retry path")
    label = bridge.snapshot(sid).cards[0].label
    assert label.theme.value == "bug" and label.criticality.value == "high"
    assert label.about == "the retry path" and label.by is Origin.AGENT


async def test_half_a_label_is_refused_rather_than_guessed(setup):
    """A theme with no weight cannot be sorted and a weight with no theme cannot be filtered."""
    manager, bridge, sid = setup
    with pytest.raises(ValueError):
        await bridge.emit_card(sid, None, "x", theme="bug")
    with pytest.raises(ValueError):
        await bridge.emit_card(sid, None, "x", criticality="high")


async def test_no_label_at_all_is_fine(setup):
    manager, bridge, sid = setup
    await bridge.emit_card(sid, None, "just context, not a finding")
    assert bridge.snapshot(sid).cards[0].label is None


async def test_a_theme_it_invented_is_rejected(setup):
    manager, bridge, sid = setup
    with pytest.raises(ValueError):
        await bridge.emit_card(sid, None, "x", theme="cleanliness", criticality="low")


async def test_it_can_reclassify_what_it_already_posted(setup):
    manager, bridge, sid = setup
    await bridge.emit_card(sid, None, "x", theme="style", criticality="low")
    cid = bridge.snapshot(sid).cards[0].id
    await bridge.label_card(sid, cid, theme="security", criticality="high",
                            about="reachable from the public API")
    label = bridge.snapshot(sid).cards[0].label
    assert label.theme.value == "security" and label.about == "reachable from the public API"


async def test_the_agent_records_what_it_changed_and_against_what(setup):
    manager, bridge, sid = setup
    await _add_highlight(manager, sid)
    hid = bridge.snapshot(sid).highlights[0].id
    await bridge.record_addressed(sid, "highlight", hid, "def456", "bounded the retry at five")
    record = bridge.snapshot(sid).addressed[0]
    assert record.subject.id == hid and record.sha == "def456"
    assert record.summary == "bounded the retry at five"


async def test_the_agent_can_record_a_fix_to_its_own_finding(setup):
    manager, bridge, sid = setup
    await bridge.emit_card(sid, None, "the retry is unbounded")
    cid = bridge.snapshot(sid).cards[0].id
    await bridge.record_addressed(sid, "insight", cid, "ccc333")
    assert bridge.snapshot(sid).addressed[0].subject.id == cid
