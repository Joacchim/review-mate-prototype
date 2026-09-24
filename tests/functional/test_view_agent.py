"""The agent's read of a session: the same folded views the reviewer's clients get.

What matters is not that fields arrive but that the right ones do. The agent was the only party
reading raw session state, so it re-derived facts the scopes already publish and saw things nobody
had ever decided to show it.
"""
import pytest

from conftest import HostStub
from review_mate.seams import MRRef
from review_mate.session.commands import (
    AddHighlight, DecideAccess, PostMessage, RequestAccess, RequestContext, SaveDraft,
)
from review_mate.session.manager import SessionManager
from review_mate.session.state import LineRange, Origin, Side
from review_mate.view.access import AccessScope
from review_mate.view.agent import AgentView
from review_mate.view.chat import ChatScopes
from review_mate.view.rail import RailScope
from review_mate.view.threads import ThreadsScope


@pytest.fixture
async def agent(tmp_path):
    made = []

    async def build():
        manager = SessionManager(root=tmp_path / "sessions", mr_source=HostStub())
        made.append(manager)
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        view = AgentView(manager, rail=RailScope(manager), chat=ChatScopes(manager),
                         threads=ThreadsScope(manager, user="reviewer"),
                         access=AccessScope(manager))
        return manager, sid, view
    yield build
    for manager in made:
        await manager.shutdown()


async def mark(actor, line=1):
    await actor.submit(AddHighlight(file="a.py", side=Side.NEW,
                                    line_range=LineRange(start=line, end=line)), Origin.BROWSER)
    return actor.snapshot().highlights[-1]


async def test_it_says_what_is_being_reviewed_and_where_to_read_it(agent):
    manager, sid, view = await agent()
    built = await view.build(sid)
    assert built["state"] == "ready"
    assert built["mr"]["project"] == "g/p" and built["mr"]["iid"] == 1
    assert "checkout_path" in built, "the agent reads code from disk, and this is where"


async def test_the_backlog_is_published_not_rederived(agent):
    """The worker used to recompute this from raw state — a third copy of the same predicate."""
    manager, sid, view = await agent()
    actor = manager.get(sid)
    highlight = await mark(actor)
    await actor.submit(RequestContext(highlight_id=highlight.id), Origin.BROWSER)
    asks = (await view.build(sid))["chat"]["agent"]["asks"]
    assert [a["kind"] for a in asks] == ["context"]


async def test_a_bare_highlight_is_on_the_rail_and_owes_nothing(agent):
    manager, sid, view = await agent()
    highlight = await mark(manager.get(sid))
    built = await view.build(sid)
    assert [h["id"] for h in built["rail"]["highlights"]] == [highlight.id]
    assert built["chat"]["agent"]["asks"] == []


async def test_the_reviewers_unsent_comment_is_not_in_it(agent):
    """A draft is private prose written expecting no reader. Once posted it is a discussion, and
    the agent reads it there like everyone else."""
    manager, sid, view = await agent()
    actor = manager.get(sid)
    highlight = await mark(actor)
    await actor.submit(SaveDraft(highlight_id=highlight.id,
                                 body="this is sloppy and the author never handles None"),
                       Origin.BROWSER)
    built = await view.build(sid)
    assert "sloppy" not in repr(built), "the agent must not read an unsent draft"
    assert "review" not in built, "no review scope at all — there is no filter to get wrong"


async def test_the_diff_is_not_in_it(agent):
    """It has its own tool: large, on a different clock, and most of the payload when bundled."""
    manager, sid, view = await agent()
    assert "files" not in await view.build(sid)


async def test_the_conversations_come_with_it(agent):
    manager, sid, view = await agent()
    await manager.get(sid).submit(PostMessage(body="why keep the legacy queue?"), Origin.BROWSER)
    rows = (await view.build(sid))["chat"]["conversations"]
    assert any(r["preview"] == "why keep the legacy queue?" for r in rows)


async def test_a_session_that_is_not_there_is_named_rather_than_raised(agent):
    manager, _sid, view = await agent()
    assert (await view.build("nope"))["state"] == "unknown-session"


# --- the consent list, on its own ---------------------------------------------

async def test_the_consent_list_reads_off_the_scope_the_reviewer_sees(agent):
    manager, sid, view = await agent()
    actor = manager.get(sid)
    await actor.submit(RequestAccess(repo="g/sibling", reason="the contract"), Origin.AGENT)
    rid = actor.snapshot().access_requests[-1].id
    await actor.submit(DecideAccess(request_id=rid, approve=False), Origin.BROWSER)

    rows = await view.access(sid)
    assert rows == [{"id": rid, "repo": "g/sibling", "reason": "the contract",
                     "status": "denied", "state": None, "path": None, "error": ""}]
    built = await view.build(sid)
    assert built["access"]["requests"][0]["status"] == "denied", "and the same fact in the view"
