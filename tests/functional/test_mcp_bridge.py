"""Functional tests for the agent bridge + MCP server wiring."""
import asyncio

import pytest

from review_mate.mcp.bridge import AgentBridge
from review_mate.mcp.server import build_mcp_server
from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight, DecideAccess, RecordGrant
from review_mate.session.state import Grant, LineRange, Origin, Side


@pytest.fixture
async def setup(tmp_path):
    manager = SessionManager(root=tmp_path / "sessions")
    bridge = AgentBridge(manager)
    sid = await manager.create()
    yield manager, bridge, sid
    await manager.shutdown()


async def _add_highlight(manager, sid, file="a.py"):
    actor = manager.get(sid)
    res = await actor.submit(
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
    row = bridge.access_state(sid)[0]
    assert row["status"] == "denied" and row["state"] is None and row["path"] is None


async def test_the_agent_reads_the_path_only_once_it_is_ready(setup):
    manager, bridge, sid = setup
    await bridge.request_access(sid, repo="g/other", reason="contract")
    rid = bridge.snapshot(sid).access_requests[0].id
    await _decide(manager, sid, rid, approve=True)
    assert bridge.access_state(sid)[0] == {
        "id": rid, "repo": "g/other", "reason": "contract", "status": "approved",
        "state": None, "path": None, "error": ""}, "approved is not yet readable"

    await manager.get(sid).submit(
        RecordGrant(request_id=rid, grant=Grant(state="materializing")), Origin.SYSTEM)
    assert bridge.access_state(sid)[0]["state"] == "materializing"
    assert bridge.access_state(sid)[0]["path"] is None

    await manager.get(sid).submit(
        RecordGrant(request_id=rid, grant=Grant(state="ready", path="/tmp/x")), Origin.SYSTEM)
    row = bridge.access_state(sid)[0]
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
