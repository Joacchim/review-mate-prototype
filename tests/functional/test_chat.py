"""Chat: browser/agent messages carry the right role; the agent can await user messages."""
import asyncio
import pytest

from review_mate.mcp.bridge import AgentBridge
from review_mate.session.manager import SessionManager
from review_mate.session.commands import (
    AddHighlight, ClearChat, EmitCard, PostMessage, Rejection, RemoveCard, RemoveHighlight,
    RequestInsights, handle,
)
from review_mate.session.state import (
    Card, Highlight, LineRange, Origin, ReviewThread, SessionState, Side, Subject, SubjectKind,
    ThreadComment,
)


@pytest.fixture
async def setup(tmp_path):
    m = SessionManager(root=tmp_path / "sessions")
    sid = await m.create()
    yield m, AgentBridge(m), sid
    await m.shutdown()


async def test_browser_message_is_user_agent_message_is_agent(setup):
    m, bridge, sid = setup
    writer = m.get(sid)
    await writer.submit(PostMessage(body="why this guard?"), Origin.BROWSER)
    await bridge.post_message(sid, "because of a race")
    msgs = writer.snapshot().messages
    assert [(x.role, x.body) for x in msgs] == [("user", "why this guard?"), ("agent", "because of a race")]


async def test_wait_for_message_returns_only_user_messages(setup):
    m, bridge, sid = setup
    writer = m.get(sid)
    waiter = asyncio.create_task(bridge.wait_for_message(sid, since=writer.snapshot().seq))
    await asyncio.sleep(0)
    await bridge.post_message(sid, "agent chatter")          # agent message must NOT wake the waiter
    await writer.submit(PostMessage(body="expand on #2"), Origin.BROWSER)
    got = await asyncio.wait_for(waiter, 1)
    assert got["message"]["role"] == "user" and got["message"]["body"] == "expand on #2"


def test_post_message_authority(setup):
    from review_mate.session.commands import handle, Rejection
    from review_mate.session.state import SessionState
    s = SessionState(id="s", created_at="t")
    assert isinstance(handle(s, PostMessage(body="x"), Origin.BROWSER), list)
    assert isinstance(handle(s, PostMessage(body="x"), Origin.AGENT), list)
    assert isinstance(handle(s, PostMessage(body="x"), Origin.SYSTEM), Rejection)


def test_clear_chat_empties_messages_browser_only():
    from review_mate.session.commands import ClearChat, handle, Rejection
    from review_mate.session.reducer import fold
    from review_mate.session.state import SessionState
    s = SessionState(id="s", created_at="t")
    s = fold(s, handle(s, PostMessage(body="hi"), Origin.BROWSER))
    s = fold(s, handle(s, PostMessage(body="hey"), Origin.AGENT))
    assert len(s.messages) == 2
    # only the browser may clear the thread
    assert isinstance(handle(s, ClearChat(), Origin.AGENT), Rejection)
    s = fold(s, handle(s, ClearChat(), Origin.BROWSER))
    assert s.messages == []


# --- chats: a message knows what it is about --------------------------


def _subject_state() -> SessionState:
    """A review with one of each thing a chat can be about."""
    return SessionState(
        id="s", created_at="t",
        highlights=[Highlight(id="h1", ordinal=1, file="a.py", side=Side.NEW,
                              line_range=LineRange(start=1, end=2))],
        cards=[Card(id="c1", highlight_id="h1", body="the answer"),
               Card(id="i1", highlight_id=None, body="an insight")],
        threads=[ReviewThread(id="d1", comments=[ThreadComment(id="t1", author="luigi", body="?")])],
    )


def _post(state, body, anchor=None, origin=Origin.BROWSER):
    return handle(state, PostMessage(body=body, anchor=anchor), origin)


def _mark(file="a.py", line=1):
    return AddHighlight(file=file, side=Side.NEW, line_range=LineRange(start=line, end=line))


async def test_a_message_can_be_about_a_highlight_an_insight_or_a_thread(setup):
    m, bridge, sid = setup
    writer = m.get(sid)
    await writer.submit(_mark(), Origin.BROWSER)
    hl = writer.snapshot().highlights[0]
    await writer.submit(EmitCard(highlight_id=None, body="an insight"), Origin.AGENT)
    insight = writer.snapshot().cards[0]

    await writer.submit(PostMessage(body="about the review"), Origin.BROWSER)
    await writer.submit(PostMessage(body="about this range",
                                   anchor=Subject(kind=SubjectKind.HIGHLIGHT, id=hl.id)),
                       Origin.BROWSER)
    await writer.submit(PostMessage(body="about that finding",
                                   anchor=Subject(kind=SubjectKind.INSIGHT, id=insight.id)),
                       Origin.AGENT)
    assert [(x.anchor.kind.value if x.anchor else None, x.body)
            for x in writer.snapshot().messages] == [
        (None, "about the review"), ("highlight", "about this range"),
        ("insight", "about that finding")]


def test_a_subject_that_does_not_exist_is_rejected():
    """An anchored message no rail row carries would never be rendered by anything."""
    state = _subject_state()
    for kind, reason in ((SubjectKind.HIGHLIGHT, "no such highlight: nope"),
                         (SubjectKind.INSIGHT, "no such insight: nope"),
                         (SubjectKind.THREAD, "no such thread: nope")):
        outcome = _post(state, "hello", Subject(kind=kind, id="nope"))
        assert isinstance(outcome, Rejection) and outcome.reason == reason


def test_a_highlights_own_card_is_not_an_insight():
    """`insight` means a card anchored to nothing — a highlight's answer is discussed at its own #N."""
    outcome = _post(_subject_state(), "hello", Subject(kind=SubjectKind.INSIGHT, id="c1"))
    assert isinstance(outcome, Rejection) and outcome.reason == "no such insight: c1"


def test_each_subject_that_exists_is_accepted():
    state = _subject_state()
    for kind, ident in ((SubjectKind.HIGHLIGHT, "h1"), (SubjectKind.INSIGHT, "i1"),
                        (SubjectKind.THREAD, "d1")):
        assert not isinstance(_post(state, "hello", Subject(kind=kind, id=ident)), Rejection)


async def test_clearing_one_conversation_leaves_the_others(setup):
    m, bridge, sid = setup
    writer = m.get(sid)
    await writer.submit(_mark(), Origin.BROWSER)
    hl = Subject(kind=SubjectKind.HIGHLIGHT, id=writer.snapshot().highlights[0].id)
    await writer.submit(PostMessage(body="general"), Origin.BROWSER)
    await writer.submit(PostMessage(body="anchored", anchor=hl), Origin.BROWSER)

    await writer.submit(ClearChat(), Origin.BROWSER)
    assert [x.body for x in writer.snapshot().messages] == ["anchored"]
    await writer.submit(ClearChat(anchor=hl), Origin.BROWSER)
    assert writer.snapshot().messages == []


async def test_dismissing_an_insight_discards_what_was_said_about_it(setup):
    m, bridge, sid = setup
    writer = m.get(sid)
    await writer.submit(EmitCard(highlight_id=None, body="an insight"), Origin.AGENT)
    card = writer.snapshot().cards[0]
    await writer.submit(PostMessage(body="general"), Origin.BROWSER)
    await writer.submit(PostMessage(body="about the finding",
                                   anchor=Subject(kind=SubjectKind.INSIGHT, id=card.id)),
                       Origin.BROWSER)
    await writer.submit(RemoveCard(card_id=card.id), Origin.BROWSER)
    assert [x.body for x in writer.snapshot().messages] == ["general"]


async def test_removing_a_highlight_discards_its_conversation(setup):
    m, bridge, sid = setup
    writer = m.get(sid)
    await writer.submit(_mark(), Origin.BROWSER)
    hid = writer.snapshot().highlights[0].id
    await writer.submit(PostMessage(body="about this range",
                                   anchor=Subject(kind=SubjectKind.HIGHLIGHT, id=hid)),
                       Origin.BROWSER)
    await writer.submit(RemoveHighlight(highlight_id=hid), Origin.BROWSER)
    assert writer.snapshot().messages == []


async def test_asking_for_insights_records_when(setup):
    m, bridge, sid = setup
    writer = m.get(sid)
    assert writer.snapshot().insights_requested is False
    await writer.submit(RequestInsights(), Origin.BROWSER)
    snap = writer.snapshot()
    assert snap.insights_requested is True and snap.insights_requested_at != ""


async def test_a_conversation_survives_a_replay(tmp_path):
    """Anchors are in the log, so a restart rebuilds which chat a message was in."""
    root = tmp_path / "sessions"
    m = SessionManager(root=root)
    sid = await m.create()
    writer = m.get(sid)
    await writer.submit(EmitCard(highlight_id=None, body="an insight"), Origin.AGENT)
    card = writer.snapshot().cards[0]
    await writer.submit(PostMessage(body="about the finding",
                                   anchor=Subject(kind=SubjectKind.INSIGHT, id=card.id)),
                       Origin.BROWSER)
    await m.shutdown()

    restored = SessionManager(root=root)
    await restored.restore_all()
    try:
        assert restored.get(sid).snapshot().messages[-1].anchor == \
            Subject(kind=SubjectKind.INSIGHT, id=card.id)
    finally:
        await restored.shutdown()


async def test_the_agent_answers_in_the_conversation_it_was_asked_in(setup):
    """The agent's own door: the anchor it received on a message is the anchor it replies with."""
    m, bridge, sid = setup
    writer = m.get(sid)
    await writer.submit(_mark(), Origin.BROWSER)
    hl = Subject(kind=SubjectKind.HIGHLIGHT, id=writer.snapshot().highlights[0].id)
    waiter = asyncio.create_task(bridge.wait_for_message(sid, since=writer.snapshot().seq))
    await asyncio.sleep(0)
    await writer.submit(PostMessage(body="does anything read this?", anchor=hl), Origin.BROWSER)
    asked = await asyncio.wait_for(waiter, 1)
    assert asked["message"]["anchor"] == {"kind": "highlight", "id": hl.id}

    await bridge.post_message(sid, "two call sites in tests/", Subject(**asked["message"]["anchor"]))
    answer = writer.snapshot().messages[-1]
    assert answer.role == "agent" and answer.anchor == hl
