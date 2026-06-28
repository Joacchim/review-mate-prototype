"""The MR-lookup channel: broker pub/sub, the agent bridge surface, and the HTTP routes."""
import asyncio

import httpx
import pytest
from httpx import ASGITransport

from review_mate.lookup.broker import LookupBroker
from review_mate.mcp.bridge import AgentBridge
from review_mate.mcp.server import build_mcp_server
from review_mate.server.app import create_app
from review_mate.session.manager import SessionManager


# --- broker -----------------------------------------------------------------

async def test_answer_wakes_a_waiting_browser():
    broker = LookupBroker()
    req = broker.create("the cache MR")
    waiter = asyncio.create_task(broker.wait_for_answer(req.id, timeout=1))
    await asyncio.sleep(0)
    broker.answer(req.id, "probably !573", [{"project": "g/p", "iid": 573}])
    got = await asyncio.wait_for(waiter, 1)
    assert got.status == "answered" and got.candidates[0]["iid"] == 573


async def test_wait_for_request_returns_pending_then_blocks_for_next():
    broker = LookupBroker()
    first = broker.create("q1")
    # a request already present with seq>since returns immediately
    got = await broker.wait_for_request(since=0, timeout=1)
    assert got.id == first.id
    # past it, the waiter blocks until the next create()
    waiter = asyncio.create_task(broker.wait_for_request(since=first.seq, timeout=1))
    await asyncio.sleep(0)
    second = broker.create("q2")
    nxt = await asyncio.wait_for(waiter, 1)
    assert nxt.id == second.id


async def test_wait_for_answer_times_out_to_pending():
    broker = LookupBroker()
    req = broker.create("unanswered")
    got = await broker.wait_for_answer(req.id, timeout=0.05)
    assert got.status == "pending"  # caller re-polls or falls back


def test_answer_unknown_raises():
    with pytest.raises(KeyError):
        LookupBroker().answer("nope", "x")


# --- agent bridge -----------------------------------------------------------

async def test_bridge_lookup_roundtrip_and_search():
    class StubProvider:
        async def search(self, q):
            return [{"host": "gitlab", "project": "g/p", "iid": 7, "title": f"hit:{q}", "url": "u"}]

    broker = LookupBroker()
    bridge = AgentBridge(SessionManager(), broker=broker, provider=StubProvider())
    req = broker.create("find it")
    got = await bridge.wait_for_lookup(since=0, timeout=1)
    assert got["query"] == "find it" and got["id"] == req.id
    cands = await bridge.search_mrs("cache")
    assert cands[0]["title"] == "hit:cache"
    answered = bridge.answer_lookup(req.id, "this one", cands)
    assert answered["status"] == "answered" and answered["candidates"][0]["iid"] == 7


async def test_bridge_lookup_noop_without_broker():
    bridge = AgentBridge(SessionManager())
    assert await bridge.wait_for_lookup(since=0, timeout=0.05) is None
    assert await bridge.search_mrs("x") == []


async def test_lookup_tools_registered():
    bridge = AgentBridge(SessionManager(), broker=LookupBroker())
    names = {t.name for t in await build_mcp_server(bridge).list_tools()}
    assert {"wait_for_lookup", "answer_lookup", "search_mrs"} <= names


# --- HTTP routes ------------------------------------------------------------

async def test_lookup_routes_open_poll_and_answer(tmp_path):
    manager = SessionManager(root=tmp_path / "sessions")
    app = create_app(manager=manager, with_mcp=False)  # broker is built internally, on app.state
    broker = app.state.broker
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        opened = await c.post("/api/lookup", json={"query": "the cache MR"})
        assert opened.status_code == 200
        lid = opened.json()["id"]
        # poll (long-poll) while the agent answers through the shared broker
        poll = asyncio.create_task(c.get(f"/api/lookup/{lid}"))
        await asyncio.sleep(0)
        broker.answer(lid, "probably !573", [{"project": "g/p", "iid": 573, "title": "cache"}])
        res = (await asyncio.wait_for(poll, 2)).json()
        assert res["status"] == "answered" and res["candidates"][0]["iid"] == 573
        blank = await c.post("/api/lookup", json={"query": "   "})
        assert blank.status_code == 400
    await manager.shutdown()
