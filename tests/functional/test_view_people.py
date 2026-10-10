"""The `people` topic: who a reviewer can put a name to.

A mention reaches someone only if the handle is exact, so these are about offering a real one. Two
sources that are not the same kind of answer — the people already on the change, which the session
holds and which cannot go stale, and the project's members, which is a host read that is often
refused outright. The second rides behind the first and says so separately.
"""
import pytest

from conftest import HostStub
from review_mate.contracts import MRRef
from review_mate.session.commands import ReplaceThreads
from review_mate.session.manager import SessionManager
from review_mate.session.state import Origin, ReviewThread, ThreadComment
from review_mate.view.people import PeopleTopic


class Peopled(HostStub):
    """A forge that can say who is in the project — or refuses, which GitHub usually does."""

    def __init__(self, roster=None, fail=None, **kw):
        super().__init__(**kw)
        self.roster = roster if roster is not None else [
            {"username": "john.doe", "name": "John Doe"},
            {"username": "jdupont", "name": "Jean Dupont"}]
        self.fail = fail
        self.asked = 0

    async def load(self, ref):
        payload = await super().load(ref)
        payload.mr.capabilities = {**(payload.mr.capabilities or {}), "members": True}
        return payload

    async def members(self, ref):
        self.asked += 1
        if self.fail is not None:
            raise self.fail
        return list(self.roster)


@pytest.fixture
async def people(tmp_path):
    published = []

    async def build(host=None, threads=()):
        host = host if host is not None else Peopled()
        manager = SessionManager(root=tmp_path / "sessions", mr_source=host)
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        if threads:
            await manager.get(sid).submit(ReplaceThreads(threads=list(threads)), Origin.SYSTEM)

        async def publish(topic):
            published.append(topic)

        return manager, sid, PeopleTopic(manager, provider=host, publish=publish), host, published
    yield build


def _said(author, id="t1"):
    return ReviewThread(id=id, comments=[ThreadComment(id="c1", author=author, body="…")])


# --- the people the session already holds -----------------------------------

async def test_the_people_on_the_change_need_no_host_read(people):
    """The author and whoever has written here are exact handles already in hand, and they are
    most of what a reviewer mentions. Nothing is asked of the forge to offer them."""
    manager, sid, topic, host, _ = await people(threads=[_said("eric")])
    view = await topic.build(sid)

    assert host.asked == 0
    on_change = [p["username"] for p in view["people"] if p["on_change"]]
    assert "eric" in on_change
    assert view["members"] == "idle", "and the project's own list has not been asked for"
    await manager.shutdown()


async def test_the_author_comes_first(people):
    """A review comment is usually addressed to them, so they are the first name offered."""
    manager, sid, topic, _, _ = await people(threads=[_said("eric")])
    view = await topic.build(sid)
    assert view["people"][0]["on_change"] is True
    await manager.shutdown()


# --- the project's own list -------------------------------------------------

async def test_fetching_adds_the_members_behind_them(people):
    manager, sid, topic, host, _ = await people(threads=[_said("eric")])
    await topic.fetch(sid)
    view = await topic.build(sid)

    assert view["members"] == "ready" and host.asked == 1
    names = [p["username"] for p in view["people"]]
    assert "eric" in names and "jdupont" in names
    assert [p["username"] for p in view["people"] if p["on_change"]][0] == names[0], \
        "the people on the change stay in front of the rest"
    await manager.shutdown()


async def test_somebody_on_the_change_is_not_listed_twice(people):
    """The author is usually a member too, and a list with them in it twice is a list that reads
    as two different people with one name."""
    manager, sid, topic, host, _ = await people()
    host.roster = [{"username": "john.doe", "name": "John Doe"}]
    await manager.get(sid).submit(
        ReplaceThreads(threads=[_said("john.doe")]), Origin.SYSTEM)

    await topic.fetch(sid)
    view = await topic.build(sid)
    assert [p["username"] for p in view["people"]].count("john.doe") == 1
    await manager.shutdown()


async def test_the_list_is_read_once(people):
    manager, sid, topic, host, _ = await people()
    await topic.fetch(sid)
    await topic.fetch(sid)
    assert host.asked == 1
    await manager.shutdown()


async def test_a_forge_that_refuses_still_leaves_the_people_on_the_change(people):
    """GitHub asks for push access to say who a repository's collaborators are, which most
    reviewers do not have. The mention list has to keep working without it."""
    manager, sid, topic, _, _ = await people(host=Peopled(fail=RuntimeError("403 push access")),
                                             threads=[_said("eric")])
    await topic.fetch(sid)
    view = await topic.build(sid)

    assert view["members"] == "error" and "403" in view["error"]
    assert "eric" in [p["username"] for p in view["people"]]
    await manager.shutdown()


async def test_a_host_that_cannot_list_members_says_so_rather_than_looking_empty(people):
    """`unavailable` and `ready with nobody in it` are different facts, and the second is what a
    client would warn on."""
    manager, sid, topic, _, _ = await people(host=HostStub())      # no members method
    view = await topic.build(sid)
    assert view["members"] == "unavailable"
    await manager.shutdown()


async def test_fetching_republishes_so_the_list_fills_in_under_the_cursor(people):
    manager, sid, topic, _, published = await people()
    await topic.fetch(sid)
    assert published.count(f"people:{sid}") == 2, "loading, then the answer"
    await manager.shutdown()


async def test_a_session_that_is_not_there_says_unknown_session(people):
    manager, _, topic, _, _ = await people()
    assert (await topic.build("nope"))["state"] == "unknown-session"
    await manager.shutdown()
