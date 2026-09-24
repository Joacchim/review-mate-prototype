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
from review_mate.session.commands import (
    AddHighlight, ApplyMRMetadata, EmitCard, LabelCard, RequestInsights, SaveDraft,
)
from review_mate.session.manager import SessionManager
from review_mate.session.state import Criticality, Label, LineRange, Origin, Side, Theme
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


async def test_a_number_survives_the_removal_of_an_earlier_highlight(session):
    """#N is a reference a reviewer uses in conversation and an agent cites in a card. If removing
    one renumbered the rest, a written "#3" would quietly point at a different piece of code."""
    from review_mate.session.commands import RemoveHighlight

    manager, sid, provider = session
    actor = manager.get(sid)
    for n in range(3):
        await actor.submit(highlight(f"f{n}.py", n + 1, n + 1), Origin.BROWSER)
    rail = rail_for(manager, provider)
    assert [(h["n"], h["file"]) for h in (await rail.build(sid))["highlights"]] == \
        [(1, "f0.py"), (2, "f1.py"), (3, "f2.py")]

    second = actor.snapshot().highlights[1]
    await actor.submit(RemoveHighlight(highlight_id=second.id), Origin.BROWSER)
    assert [(h["n"], h["file"]) for h in (await rail.build(sid))["highlights"]] == \
        [(1, "f0.py"), (3, "f2.py")]        # a gap, not a renumber


async def test_a_number_is_never_reused_after_the_newest_is_removed(session):
    """The trap in the cheap fix: max(existing) + 1 would hand #3 to a different highlight, so a
    stale reference would retarget instead of dangling."""
    from review_mate.session.commands import RemoveHighlight

    manager, sid, provider = session
    actor = manager.get(sid)
    await actor.submit(highlight("a.py", 1, 1), Origin.BROWSER)
    await actor.submit(highlight("b.py", 2, 2), Origin.BROWSER)
    newest = actor.snapshot().highlights[-1]
    await actor.submit(RemoveHighlight(highlight_id=newest.id), Origin.BROWSER)
    await actor.submit(highlight("c.py", 3, 3), Origin.BROWSER)
    rail = rail_for(manager, provider)
    assert [(h["n"], h["file"]) for h in (await rail.build(sid))["highlights"]] == \
        [(1, "a.py"), (3, "c.py")]


async def test_removing_a_card_leaves_the_numbering_alone(session):
    """Cards are not what is numbered — only a reviewer dismissing an insight should change."""
    from review_mate.session.commands import EmitCard, RemoveCard

    manager, sid, provider = session
    actor = manager.get(sid)
    await actor.submit(highlight("a.py", 1, 1), Origin.BROWSER)
    await actor.submit(highlight("b.py", 2, 2), Origin.BROWSER)
    first = actor.snapshot().highlights[0]
    await actor.submit(EmitCard(highlight_id=first.id, body="an answer"), Origin.AGENT)
    card = actor.snapshot().cards[0]
    # the agent may not retract a card; the reviewer dismisses it
    assert not (await actor.submit(RemoveCard(card_id=card.id), Origin.AGENT)).ok
    assert (await actor.submit(RemoveCard(card_id=card.id), Origin.BROWSER)).ok
    rail = rail_for(manager, provider)
    view = await rail.build(sid)
    assert [h["n"] for h in view["highlights"]] == [1, 2]
    assert view["highlights"][0]["card"] is None


async def test_numbering_survives_a_restart(tmp_path):
    """The counter is state, so replay must reproduce it — otherwise a restart renumbers."""
    from review_mate.session.commands import RemoveHighlight

    provider = BlameHost()
    root = tmp_path / "sessions"
    manager = SessionManager(root=root, mr_source=provider)
    sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
    actor = manager.get(sid)
    for n in range(3):
        await actor.submit(highlight(f"f{n}.py", n + 1, n + 1), Origin.BROWSER)
    await actor.submit(RemoveHighlight(highlight_id=actor.snapshot().highlights[1].id),
                       Origin.BROWSER)
    await manager.shutdown()

    restored = SessionManager(root=root, mr_source=provider)
    await restored.restore_all()
    view = await RailScope(restored, provider=provider).build(sid)
    assert [h["n"] for h in view["highlights"]] == [1, 3]
    # and the next highlight continues past the gap rather than filling it
    await restored.get(sid).submit(highlight("new.py", 9, 9), Origin.BROWSER)
    view = await RailScope(restored, provider=provider).build(sid)
    assert [h["n"] for h in view["highlights"]] == [1, 3, 4]
    await restored.shutdown()


async def test_the_rail_says_who_asked_and_whether_it_was_escalated(session):
    """A highlight the agent made reads differently, and an escalated one is awaiting an answer."""
    from review_mate.session.commands import RequestContext

    manager, sid, provider = session
    actor = manager.get(sid)
    await actor.submit(highlight("a.py", 1, 1), Origin.BROWSER)
    await actor.submit(highlight("b.py", 2, 2), Origin.AGENT)
    rail = rail_for(manager, provider)
    view = await rail.build(sid)
    assert [h["author"] for h in view["highlights"]] == ["browser", "agent"]
    assert [h["context_requested"] for h in view["highlights"]] == [False, False]

    await actor.submit(RequestContext(highlight_id=actor.snapshot().highlights[0].id),
                       Origin.BROWSER)
    view = await rail.build(sid)
    assert view["highlights"][0]["context_requested"] is True
    # the escalation's own timestamp rides along: it is what ages the "Claude is working" cue
    assert view["highlights"][0]["context_requested_at"] == (
        actor.snapshot().highlights[0].context_requested_at)
    assert view["highlights"][0]["context_requested_at"] != ""


# --- the MR-wide review pass --------------------------------------------------
# `stale` and `available` answer different questions, and conflating them is what would make a
# waiting cue disappear with nothing arriving: one says the pass is about code that moved, the
# other says the code in front of the reviewer has not been passed over.

async def test_a_review_nobody_has_asked_about_can_be_asked_about(session):
    manager, sid, provider = session
    scope = rail_for(manager, provider)
    passed = (await scope.build(sid))["review_pass"]
    assert passed["requested"] is False and passed["available"] is True


async def test_a_pass_in_flight_cannot_be_asked_for_again(session):
    manager, sid, provider = session
    scope = rail_for(manager, provider)
    await manager.get(sid).submit(RequestInsights(), Origin.BROWSER)
    passed = (await scope.build(sid))["review_pass"]
    assert passed["requested"] is True and passed["stale"] is False
    assert passed["available"] is False


async def test_a_pass_the_change_moved_past_stays_visible_and_reads_stale(session):
    manager, sid, provider = session
    scope = rail_for(manager, provider)
    await manager.get(sid).submit(RequestInsights(), Origin.BROWSER)
    snapshot = manager.get(sid).snapshot()
    await manager.get(sid).submit(
        ApplyMRMetadata(mr=snapshot.mr.model_copy(update={"sha": "moved-on"})), Origin.SYSTEM)

    passed = (await scope.build(sid))["review_pass"]
    assert passed["requested"] is True, "it must not vanish — a cue that stops with nothing said"
    assert passed["stale"] is True
    assert passed["available"] is True, "the code in front of the reviewer has not been passed over"


async def test_asking_again_about_the_new_code_is_no_longer_stale(session):
    manager, sid, provider = session
    scope = rail_for(manager, provider)
    await manager.get(sid).submit(RequestInsights(), Origin.BROWSER)
    snapshot = manager.get(sid).snapshot()
    await manager.get(sid).submit(
        ApplyMRMetadata(mr=snapshot.mr.model_copy(update={"sha": "moved-on"})), Origin.SYSTEM)
    await manager.get(sid).submit(RequestInsights(), Origin.BROWSER)

    passed = (await scope.build(sid))["review_pass"]
    assert passed["stale"] is False and passed["available"] is False
    assert passed["sha"] == "moved-on"


# --- what an insight is about --------------------------------------------------

async def test_an_insight_carries_its_label(session):
    manager, sid, provider = session
    scope = rail_for(manager, provider)
    await manager.get(sid).submit(EmitCard(
        highlight_id=None, body="the retry is unbounded",
        label=Label(theme=Theme.BUG, criticality=Criticality.HIGH, about="the retry path")),
        Origin.AGENT)
    label = (await scope.build(sid))["insights"][0]["label"]
    assert label == {"theme": "bug", "criticality": "high", "about": "the retry path",
                     "by": "agent"}


async def test_an_unclassified_insight_says_nothing_rather_than_low(session):
    """Absent must not read as unimportant — it means nobody looked at it that way."""
    manager, sid, provider = session
    scope = rail_for(manager, provider)
    await manager.get(sid).submit(EmitCard(highlight_id=None, body="a note"), Origin.AGENT)
    assert (await scope.build(sid))["insights"][0]["label"] is None


async def test_a_corrected_label_says_whose_it_is_now(session):
    """A client shows a reviewer's correction differently from a claim nobody questioned."""
    manager, sid, provider = session
    scope = rail_for(manager, provider)
    actor = manager.get(sid)
    await actor.submit(EmitCard(highlight_id=None, body="x", label=Label(
        theme=Theme.BUG, criticality=Criticality.HIGH)), Origin.AGENT)
    cid = actor.snapshot().cards[0].id
    await actor.submit(LabelCard(card_id=cid, label=Label(
        theme=Theme.STYLE, criticality=Criticality.LOW)), Origin.BROWSER)
    label = (await scope.build(sid))["insights"][0]["label"]
    assert label["theme"] == "style" and label["by"] == "browser"
