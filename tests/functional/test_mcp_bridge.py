"""Functional tests for the agent bridge + MCP server wiring."""
import asyncio

import pytest

from review_mate.mcp.bridge import AgentBridge
from review_mate.mcp.server import build_mcp_server
from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight
from review_mate.session.state import Origin, Side, LineRange


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
                     "emit_card", "request_access"):
        assert expected in names
