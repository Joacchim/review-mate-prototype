"""The rail scope: what a reviewer asked about, and what came back.

The cross-file view is why this is one scope rather than many — the numbering a reviewer references
is session-wide, and no per-file view can assign it.

Most of this drives RailScope directly. The scope's own logic needs no transport, and a sync
TestClient runs the application on another loop, so an actor reached from a test coroutine would be
touching primitives that belong to a different one. The tail is the exception and is tested through
the real stream, because that is the thing being checked.
"""
import json

import pytest
from starlette.testclient import TestClient

from conftest import HostStub
from review_mate.seams import MRRef
from review_mate.server.app import create_app
from review_mate.session.commands import AddHighlight, EmitCard, SaveDraft
from review_mate.session.manager import SessionManager
from review_mate.session.state import LineRange, Origin, Side
from review_mate.view.rail import RailScope


class BlameHost(HostStub):
    """A host that can answer the cheap tier."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.blame_calls = []

    async def blame(self, project, path, ref, start, end):
        self.blame_calls.append((path, ref, start, end))
        return [{"author": "luigi", "date": "2026-01-01", "line": start}]

    async def linked_issues(self, project, iid):
        return [{"iid": 322, "title": "reserve capacity"}]


def highlight(file="a.py", start=10, end=12, question=None):
    return AddHighlight(file=file, side=Side.NEW, line_range=LineRange(start=start, end=end),
                        question=question)


@pytest.fixture
async def session(tmp_path):
    """A loaded session, its manager, and its host — all on this test's loop."""
    provider = BlameHost()
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
    yield manager, sid, provider
    await manager.shutdown()


def rail_for(manager, provider, published=None):
    return RailScope(manager, provider=provider,
                     publish=None if published is None else
                     (lambda session_id: published.append(session_id) or _done()))


async def _done():
    return None


async def test_highlights_carry_session_wide_numbering(session):
    manager, sid, provider = session
    actor = manager.get(sid)
    for command in (highlight("a.py", 10, 12), highlight("pkg/b.py", 3, 3), highlight("a.py", 40, 41)):
        await actor.submit(command, Origin.BROWSER)
    view = await rail_for(manager, provider).build(sid)
    assert [h["n"] for h in view["highlights"]] == [1, 2, 3]
    # the numbering spans files, which is why it cannot come from a per-file scope
    assert [h["file"] for h in view["highlights"]] == ["a.py", "pkg/b.py", "a.py"]


async def test_a_card_lands_on_its_highlight_and_an_insight_stands_alone(session):
    manager, sid, provider = session
    actor = manager.get(sid)
    await actor.submit(highlight(), Origin.BROWSER)
    hid = actor.snapshot().highlights[0].id
    await actor.submit(EmitCard(highlight_id=hid, body="because the pool moved",
                                citations=["pool.py:12"]), Origin.AGENT)
    await actor.submit(EmitCard(highlight_id=None, body="this MR widens a lock"), Origin.AGENT)
    view = await rail_for(manager, provider).build(sid)
    assert view["highlights"][0]["card"]["body"] == "because the pool moved"
    assert view["highlights"][0]["card"]["citations"] == ["pool.py:12"]
    assert [i["body"] for i in view["insights"]] == ["this MR widens a lock"]


async def test_a_draft_moves_the_highlights_comment_state(session):
    manager, sid, provider = session
    actor = manager.get(sid)
    await actor.submit(highlight(), Origin.BROWSER)
    rail = rail_for(manager, provider)
    assert (await rail.build(sid))["highlights"][0]["comment_state"] == "context"
    hid = actor.snapshot().highlights[0].id
    await actor.submit(SaveDraft(highlight_id=hid, body="rename this"), Origin.BROWSER)
    assert (await rail.build(sid))["highlights"][0]["comment_state"] == "comment"


async def test_the_cheap_tier_loads_then_lands(session):
    manager, sid, provider = session
    await manager.get(sid).submit(highlight("a.py", 10, 12), Origin.BROWSER)
    published = []
    rail = rail_for(manager, provider, published)
    first = await rail.build(sid)
    assert first["highlights"][0]["context"]["state"] == "loading"   # build never calls the host
    await rail._tasks[("abc", "a.py", 10, 12)]
    view = await rail.build(sid)
    context = view["highlights"][0]["context"]
    assert context["state"] == "ready"
    assert context["blame"][0]["author"] == "luigi"
    assert context["linked_issues"][0]["iid"] == 322
    assert provider.blame_calls == [("a.py", "abc", 10, 12)]      # the range, at the head
    assert published == [sid]                                      # and it republished on landing


async def test_one_read_serves_a_range(session):
    manager, sid, provider = session
    await manager.get(sid).submit(highlight("a.py", 10, 12), Origin.BROWSER)
    rail = rail_for(manager, provider)
    await rail.build(sid)
    await rail._tasks[("abc", "a.py", 10, 12)]
    await rail.build(sid)
    await rail.build(sid)
    # a range at a fixed sha cannot change, so it is read once however often the rail is built
    assert provider.blame_calls.count(("a.py", "abc", 10, 12)) == 1


async def test_a_failing_blame_does_not_cost_the_linked_issues(session):
    """Each source of the cheap tier degrades on its own."""
    manager, sid, provider = session

    async def broken_blame(*args):
        raise RuntimeError("no blame here")

    provider.blame = broken_blame
    await manager.get(sid).submit(highlight(), Origin.BROWSER)
    rail = rail_for(manager, provider)
    await rail.build(sid)
    await rail._tasks[("abc", "a.py", 10, 12)]
    context = (await rail.build(sid))["highlights"][0]["context"]
    assert context["state"] == "ready" and context["blame"] == []
    assert context["linked_issues"][0]["iid"] == 322


async def test_a_host_that_cannot_blame_says_unavailable(session):
    manager, sid, _ = session
    await manager.get(sid).submit(highlight(), Origin.BROWSER)
    view = await RailScope(manager, provider=HostStub()).build(sid)
    assert view["highlights"][0]["context"]["state"] == "unavailable"


async def test_an_unknown_session_is_reported(session):
    manager, _, provider = session
    assert (await rail_for(manager, provider).build("ghost"))["state"] == "unknown-session"


# --- the tail, through the real stream ---------------------------------------

def test_the_rail_republishes_when_the_session_changes(tmp_path):
    """Without the tail on a watched session's events, a card would never reach a client."""
    provider = BlameHost()
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    app = create_app(manager=manager, provider=provider, with_mcp=False,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))
    with TestClient(app) as tc:
        sid = tc.post("/api/cmd", json={"cmd": "session.open",
                                        "args": {"ref": "g/p!1"}}).json()["session"]
        with tc.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "scopes": [f"rail:{sid}"]})
            assert json.loads(ws.receive_text())["view"]["highlights"] == []
            tc.post(f"/api/sessions/{sid}/commands",
                    json={"type": "add_highlight", "file": "a.py", "side": "new",
                          "line_range": {"start": 10, "end": 12}})
            for _ in range(6):
                view = json.loads(ws.receive_text())["view"]
                if view.get("highlights"):
                    assert view["highlights"][0]["n"] == 1
                    return
            raise AssertionError("the rail never republished")


async def test_a_highlight_made_against_an_older_head_is_marked_stale(tmp_path):
    """Its lines may have moved since, so the client must not present it as still exact."""
    from review_mate.session.commands import ApplyMRMetadata
    from review_mate.session.state import MRMetadata

    provider = BlameHost()
    manager = SessionManager(root=tmp_path / "sessions", mr_source=provider)
    sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
    actor = manager.get(sid)
    await actor.submit(highlight(), Origin.BROWSER)
    rail = RailScope(manager, provider=provider)
    assert (await rail.build(sid))["highlights"][0]["stale"] is False

    moved = actor.snapshot().mr.model_copy(update={"sha": "moved-on"})
    await actor.submit(ApplyMRMetadata(mr=moved), Origin.SYSTEM)
    assert (await rail.build(sid))["highlights"][0]["stale"] is True
    await manager.shutdown()
