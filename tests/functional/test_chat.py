"""Chat: browser/agent messages carry the right role; the agent can await user messages."""
import asyncio
import pytest

from review_mate.mcp.bridge import AgentBridge
from review_mate.session.manager import SessionManager
from review_mate.session.commands import PostMessage
from review_mate.session.state import Origin


@pytest.fixture
async def setup(tmp_path):
    m = SessionManager(root=tmp_path / "sessions")
    sid = await m.create()
    yield m, AgentBridge(m), sid
    await m.shutdown()


async def test_browser_message_is_user_agent_message_is_agent(setup):
    m, bridge, sid = setup
    actor = m.get(sid)
    await actor.submit(PostMessage(body="why this guard?"), Origin.BROWSER)
    await bridge.post_message(sid, "because of a race")
    msgs = actor.snapshot().messages
    assert [(x.role, x.body) for x in msgs] == [("user", "why this guard?"), ("agent", "because of a race")]


async def test_wait_for_message_returns_only_user_messages(setup):
    m, bridge, sid = setup
    actor = m.get(sid)
    waiter = asyncio.create_task(bridge.wait_for_message(sid, since=actor.snapshot().seq))
    await asyncio.sleep(0)
    await bridge.post_message(sid, "agent chatter")          # agent message must NOT wake the waiter
    await actor.submit(PostMessage(body="expand on #2"), Origin.BROWSER)
    got = await asyncio.wait_for(waiter, 1)
    assert got["message"]["role"] == "user" and got["message"]["body"] == "expand on #2"


def test_post_message_authority(setup):
    from review_mate.session.commands import handle, Rejection
    from review_mate.session.state import SessionState
    s = SessionState(id="s", created_at="t")
    assert isinstance(handle(s, PostMessage(body="x"), Origin.BROWSER), list)
    assert isinstance(handle(s, PostMessage(body="x"), Origin.AGENT), list)
    assert isinstance(handle(s, PostMessage(body="x"), Origin.SYSTEM), Rejection)
