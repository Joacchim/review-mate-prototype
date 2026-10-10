"""The `threads` topic: the discussions already on the merge request.

What a reviewer needs to answer one is here — where it sits, who said what, whether it is settled,
and which comments are theirs to edit. That last one was a question a client asked separately and
then answered by comparing usernames, which is a rule two clients can hold differently and the host
then contradicts. It is the server's answer now, and most of what is pinned here is that.
"""
import pytest

from conftest import HostStub
from review_mate.contracts import MRRef
from review_mate.session.commands import ReplaceThreads
from review_mate.session.manager import SessionManager
from review_mate.session.state import Origin, ReviewThread, ThreadComment
from review_mate.view.threads import ThreadsTopic


def thread(id="t1", resolved=False, anchor=None, comments=(), capabilities=None):
    return ReviewThread(id=id, resolved=resolved, anchor=anchor,
                        capabilities=capabilities or {},
                        comments=[ThreadComment(id=c[0], author=c[1], body=c[2])
                                  for c in comments])


@pytest.fixture
async def threads(tmp_path):
    async def build(user="reviewer", staged=(), kb=None):
        manager = SessionManager(root=tmp_path / "sessions", mr_source=HostStub())
        sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
        if staged:
            await manager.get(sid).submit(ReplaceThreads(threads=list(staged)), Origin.SYSTEM)
        return manager, sid, ThreadsTopic(manager, user=user, kb=kb)
    yield build


# --- what is on the merge request -------------------------------------------

async def test_a_review_with_no_discussions_says_so(threads):
    manager, sid, topic = await threads()
    view = await topic.build(sid)
    assert view["state"] == "ready"
    assert view["threads"] == [] and view["total"] == 0 and view["unresolved"] == 0
    await manager.shutdown()


async def test_a_discussion_carries_where_it_sits_and_what_was_said(threads):
    anchor = {"file": "a.py", "side": "new", "line": 42}
    manager, sid, topic = await threads(staged=[
        thread(anchor=anchor, comments=[("1", "eric", "prefer a guard"),
                                        ("2", "reviewer", "agreed")])])
    row = (await topic.build(sid))["threads"][0]
    assert row["anchor"] == anchor and row["resolved"] is False
    assert [(c["author"], c["body"]) for c in row["comments"]] == [
        ("eric", "prefer a guard"), ("reviewer", "agreed")]
    await manager.shutdown()


async def test_a_discussion_about_the_whole_change_is_anchored_to_nothing(threads):
    manager, sid, topic = await threads(staged=[thread(anchor=None)])
    assert (await topic.build(sid))["threads"][0]["anchor"] is None
    await manager.shutdown()


async def test_the_counts_split_settled_from_open(threads):
    manager, sid, topic = await threads(staged=[
        thread(id="t1"), thread(id="t2", resolved=True), thread(id="t3")])
    view = await topic.build(sid)
    assert view["total"] == 3 and view["unresolved"] == 2
    await manager.shutdown()


async def test_what_the_host_allows_on_a_discussion_travels_with_it(threads):
    """A client offers a reply where the host takes one, rather than guessing from the MR."""
    manager, sid, topic = await threads(staged=[
        thread(capabilities={"threads": True, "resolve": False})])
    assert (await topic.build(sid))["threads"][0]["capabilities"] == {
        "threads": True, "resolve": False}
    await manager.shutdown()


# --- whose comment is it ----------------------------------------------------

async def test_the_reviewers_own_comments_are_marked(threads):
    manager, sid, topic = await threads(user="reviewer", staged=[
        thread(comments=[("1", "eric", "prefer a guard"), ("2", "reviewer", "agreed")])])
    mine = {c["author"]: c["mine"] for c in (await topic.build(sid))["threads"][0]["comments"]}
    assert mine == {"eric": False, "reviewer": True}
    await manager.shutdown()


async def test_with_no_reviewer_known_nothing_is_theirs(threads):
    """A host that never said who is reviewing must not make every comment editable."""
    manager, sid, topic = await threads(user="", staged=[
        thread(comments=[("1", "", "a note with no author")])])
    assert (await topic.build(sid))["threads"][0]["comments"][0]["mine"] is False
    await manager.shutdown()


# --- the edges ---------------------------------------------------------------

async def test_a_session_that_is_not_there_says_unknown_session(threads):
    manager, _sid, topic = await threads()
    assert (await topic.build("no-such-session"))["state"] == "unknown-session"
    await manager.shutdown()


async def test_a_re_sync_that_drops_a_discussion_drops_it_here(threads):
    """The host is the single source of truth, so the list reconciles wholesale."""
    manager, sid, topic = await threads(staged=[thread(id="t1"), thread(id="t2")])
    assert (await topic.build(sid))["total"] == 2
    await manager.get(sid).submit(ReplaceThreads(threads=[thread(id="t2")]), Origin.SYSTEM)
    view = await topic.build(sid)
    assert [t["id"] for t in view["threads"]] == ["t2"] and view["total"] == 1
    await manager.shutdown()


# --- a voice the reviewer has stopped reading -------------------------------
# A tracker integration posts on every push, and its notes are real comments from a bot account —
# not the system notes the host flags, which is why they survive every filter that looks correct.

@pytest.fixture
def kb(tmp_path):
    from review_mate.kb.store import ReviewKB
    return ReviewKB(root=tmp_path / "kb")


async def test_a_muted_authors_discussion_is_kept_out_and_counted(threads, kb):
    kb.mute_author("gitlab", "linear_bot")
    manager, sid, topic = await threads(kb=kb, staged=[
        thread(id="t1", comments=[("1", "linear_bot", "moved to In Review")]),
        thread(id="t2", comments=[("2", "eric", "prefer a guard")])])

    view = await topic.build(sid)
    assert [t["id"] for t in view["threads"]] == ["t2"]
    assert view["total"] == 1, "the counts describe the list the reviewer was given"
    assert view["muted"] == 1, "and what is missing from it is said out loud"
    await manager.shutdown()


async def test_a_muted_thread_somebody_answered_stays(threads, kb):
    """The answer is about the change, and hiding the thread would take it with the noise."""
    kb.mute_author("gitlab", "linear_bot")
    manager, sid, topic = await threads(kb=kb, staged=[
        thread(id="t1", comments=[("1", "linear_bot", "moved to In Review"),
                                  ("2", "eric", "which is wrong, it is still blocked")])])

    view = await topic.build(sid)
    assert [t["id"] for t in view["threads"]] == ["t1"]
    assert view["muted"] == 0
    await manager.shutdown()


async def test_muting_is_per_forge(threads, kb):
    """A username is only unique on the forge that issued it, so muting one elsewhere would
    silence a stranger who happens to share the name."""
    kb.mute_author("github.com", "linear_bot")
    manager, sid, topic = await threads(kb=kb, staged=[
        thread(id="t1", comments=[("1", "linear_bot", "moved to In Review")])])

    view = await topic.build(sid)
    assert [t["id"] for t in view["threads"]] == ["t1"], "this review is on gitlab"
    await manager.shutdown()


async def test_unmuting_brings_them_back(threads, kb):
    kb.mute_author("gitlab", "linear_bot")
    manager, sid, topic = await threads(kb=kb, staged=[
        thread(id="t1", comments=[("1", "linear_bot", "moved to In Review")])])
    assert (await topic.build(sid))["threads"] == []

    kb.unmute_author("gitlab", "linear_bot")
    assert [t["id"] for t in (await topic.build(sid))["threads"]] == ["t1"]
    await manager.shutdown()


async def test_a_review_with_no_preferences_hides_nothing(threads):
    """The baseline this must not disturb: no kb, or nothing muted, and every discussion shows."""
    manager, sid, topic = await threads(staged=[
        thread(id="t1", comments=[("1", "linear_bot", "moved to In Review")])])
    view = await topic.build(sid)
    assert [t["id"] for t in view["threads"]] == ["t1"] and view["muted"] == 0
    await manager.shutdown()
