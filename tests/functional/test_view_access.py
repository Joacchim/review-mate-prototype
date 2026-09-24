"""The `access` scope: repositories Claude has asked to read, and what the reviewer decided.

Cross-repo context is consent-gated and agent-initiated, so the state worth publishing is what is
outstanding rather than what is readable. Decided requests stay: a reviewer wants to see that they
refused something, and an agent asking again for what was refused reads differently from one asking
the first time.
"""
import pytest

from conftest import HostStub
from review_mate.seams import MRRef
from review_mate.session.commands import DecideAccess, RequestAccess
from review_mate.session.manager import SessionManager
from review_mate.session.state import Origin
from review_mate.view.access import AccessScope


@pytest.fixture
async def access(tmp_path):
    async def build():
        manager = SessionManager(root=tmp_path / "sessions", mr_source=HostStub())
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        return manager, sid, AccessScope(manager)
    yield build


async def ask(manager, sid, repo, reason="it defines the type this calls"):
    await manager.get(sid).submit(RequestAccess(repo=repo, reason=reason), Origin.AGENT)
    return manager.get(sid).snapshot().access_requests[-1].id


async def test_a_review_nobody_has_asked_about_is_empty(access):
    manager, sid, scope = await access()
    view = await scope.build(sid)
    assert view["state"] == "ready" and view["requests"] == [] and view["pending"] == 0
    await manager.shutdown()


async def test_an_ask_carries_the_repo_and_why(access):
    manager, sid, scope = await access()
    await ask(manager, sid, "platform/virtu/vmdesc", reason="it defines VMDesc")
    row = (await scope.build(sid))["requests"][0]
    assert row["repo"] == "platform/virtu/vmdesc" and row["reason"] == "it defines VMDesc"
    assert row["status"] == "pending"
    await manager.shutdown()


async def test_deciding_moves_it_out_of_what_is_outstanding(access):
    manager, sid, scope = await access()
    rid = await ask(manager, sid, "platform/virtu/vmdesc")
    assert (await scope.build(sid))["pending"] == 1

    await manager.get(sid).submit(DecideAccess(request_id=rid, approve=True), Origin.BROWSER)
    view = await scope.build(sid)
    assert view["pending"] == 0
    assert view["requests"][0]["status"] == "approved"
    await manager.shutdown()


async def test_a_refusal_is_kept_rather_than_forgotten(access):
    """So a reviewer sees they refused, and an agent asking again reads as asking again."""
    manager, sid, scope = await access()
    rid = await ask(manager, sid, "platform/virtu/vmdesc")
    await manager.get(sid).submit(DecideAccess(request_id=rid, approve=False), Origin.BROWSER)
    view = await scope.build(sid)
    assert [r["status"] for r in view["requests"]] == ["denied"]
    assert view["pending"] == 0
    await manager.shutdown()


async def test_several_asks_are_counted_apart_from_the_settled_ones(access):
    manager, sid, scope = await access()
    first = await ask(manager, sid, "a/one")
    await ask(manager, sid, "a/two")
    await ask(manager, sid, "a/three")
    await manager.get(sid).submit(DecideAccess(request_id=first, approve=True), Origin.BROWSER)
    view = await scope.build(sid)
    assert len(view["requests"]) == 3 and view["pending"] == 2
    await manager.shutdown()


async def test_a_session_that_is_not_there_says_unknown_session(access):
    manager, _sid, scope = await access()
    assert (await scope.build("no-such-session"))["state"] == "unknown-session"
    await manager.shutdown()
