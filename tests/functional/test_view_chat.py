"""The chat topics: an index of a review's chats, and each chat's messages.

Split for the reason the diff is: a client subscribes to the chat it has open. The index is
also where presence stops being a raw fact and becomes an answer — "is my ask being worked on" is
the join of who is listening with what is outstanding, and no client performs it.

Most of this drives ChatTopics directly; the ticker and the republish path go through the real
stream, because the transport is the thing being checked there.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from conftest import HostStub, next_frame
from review_mate.contracts import MRRef
from review_mate.server.app import create_app
from review_mate.session.commands import (
    AddHighlight, EmitCard, PostMessage, RequestCheck, RequestContext, RequestInsights,
)
from review_mate.session.manager import SessionManager
from review_mate.session.state import LineRange, Origin, Side, Subject, SubjectKind
from review_mate.view.asks import STALE_AFTER, agent_state, outstanding
from review_mate.view.chat import ChatTopics

ATTACHED = {"attached": True, "parked": True, "last_seen": "2026-01-01T00:00:00+00:00"}
ALONE = {"attached": False, "parked": False, "last_seen": None}


@pytest.fixture
async def session(tmp_path):
    manager = SessionManager(root=tmp_path / "sessions", mr_source=HostStub())
    sid = await manager.create(ref=MRRef(host="gitlab", project="g/p", iid=1))
    yield manager, sid
    await manager.shutdown()


def chat_for(manager, watcher=None):
    return ChatTopics(manager, watcher=None if watcher is None else (lambda: watcher))


async def mark(writer, file="a.py", line=1):
    await writer.submit(AddHighlight(file=file, side=Side.NEW,
                                    line_range=LineRange(start=line, end=line)), Origin.BROWSER)
    return writer.snapshot().highlights[-1]


def rows(view):
    return {(r["kind"], r["id"]): r for r in view["chats"]}


# --- the index ---------------------------------------------------------------


async def test_a_review_with_nothing_said_still_has_its_own_conversation(session):
    manager, sid = session
    view = await chat_for(manager).build(sid)
    assert [r["kind"] for r in view["chats"]] == ["review"]
    assert view["chats"][0]["topic"] == f"chat:{sid}:review"
    assert view["chats"][0]["count"] == 0


async def test_each_conversation_is_listed_with_where_to_read_it(session):
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    await writer.submit(EmitCard(highlight_id=None, body="an insight"), Origin.AGENT)
    insight = writer.snapshot().cards[-1]
    await writer.submit(PostMessage(body="about the change"), Origin.BROWSER)
    await writer.submit(PostMessage(body="about this line",
                                   anchor=Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)),
                       Origin.BROWSER)
    await writer.submit(PostMessage(body="about that finding",
                                   anchor=Subject(kind=SubjectKind.INSIGHT, id=insight.id)),
                       Origin.AGENT)

    listed = rows(await chat_for(manager).build(sid))
    assert set(listed) == {("review", ""), ("highlight", highlight.id), ("insight", insight.id)}
    assert listed[("highlight", highlight.id)]["topic"] == f"chat:{sid}:highlight:{highlight.id}"
    assert listed[("insight", insight.id)]["preview"] == "about that finding"


async def test_the_index_says_which_chats_are_waiting_on_an_answer(session):
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    anchor = Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)
    await writer.submit(PostMessage(body="does anything read this?", anchor=anchor), Origin.BROWSER)
    await writer.submit(PostMessage(body="what is this for?"), Origin.BROWSER)
    await writer.submit(PostMessage(body="the fleet selector", anchor=None), Origin.AGENT)

    listed = rows(await chat_for(manager).build(sid))
    assert listed[("highlight", highlight.id)]["owed"] is True     # the reviewer spoke last
    assert listed[("review", "")]["owed"] is False                 # the agent answered


async def test_an_unknown_session_is_reported(session):
    manager, _ = session
    view = await chat_for(manager).build("nope")
    assert view["state"] == "unknown-session"


# --- one chat --------------------------------------------------------


async def test_a_conversation_carries_only_its_own_messages(session):
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    anchor = Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)
    await writer.submit(PostMessage(body="about the change"), Origin.BROWSER)
    await writer.submit(PostMessage(body="about this line", anchor=anchor), Origin.BROWSER)
    await writer.submit(PostMessage(body="two call sites", anchor=anchor), Origin.AGENT)

    view = await chat_for(manager).build(f"{sid}:highlight:{highlight.id}")
    assert [(m["role"], m["body"]) for m in view["messages"]] == [
        ("user", "about this line"), ("agent", "two call sites")]
    assert view["owed"] is False

    review = await chat_for(manager).build(f"{sid}:review")
    assert [m["body"] for m in review["messages"]] == ["about the change"]
    assert review["owed"] is True


async def test_a_name_that_is_not_a_conversation_is_named_rather_than_guessed(session):
    manager, sid = session
    for bad in (f"{sid}:nonsense:x", f"{sid}:highlight", f"{sid}:review:x"):
        assert (await chat_for(manager).build(bad))["state"] == "malformed-name"


# --- the agent state ---------------------------------------------------------


async def test_nothing_outstanding_reads_as_watching_or_off(session):
    manager, sid = session
    assert (await chat_for(manager, ATTACHED).build(sid))["agent"]["state"] == "watching"
    assert (await chat_for(manager, ALONE).build(sid))["agent"]["state"] == "off"


async def test_an_ask_with_nobody_listening_is_stalled_not_slow(session):
    manager, sid = session
    await manager.get(sid).submit(PostMessage(body="look?"), Origin.BROWSER)
    assert (await chat_for(manager, ATTACHED).build(sid))["agent"]["state"] == "working"
    view = await chat_for(manager, ALONE).build(sid)
    assert view["agent"]["state"] == "stalled" and view["agent"]["since"]


async def test_an_escalation_and_a_request_for_insights_are_asks_too(session):
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    await writer.submit(RequestContext(highlight_id=highlight.id), Origin.BROWSER)
    await writer.submit(RequestInsights(), Origin.BROWSER)
    kinds = [a["kind"] for a in (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"]]
    assert sorted(kinds) == ["context", "insights"]


async def test_a_bare_highlight_owes_nothing(session):
    """D21: the host context serves it, so only an explicit escalation is an ask."""
    manager, sid = session
    await mark(manager.get(sid))
    assert (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"] == []


async def test_an_answer_closes_the_ask_it_answers(session):
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    await writer.submit(RequestContext(highlight_id=highlight.id), Origin.BROWSER)
    assert (await chat_for(manager, ATTACHED).build(sid))["agent"]["state"] == "working"
    await writer.submit(EmitCard(highlight_id=highlight.id, body="here"), Origin.AGENT)
    assert (await chat_for(manager, ATTACHED).build(sid))["agent"]["state"] == "watching"


async def test_a_doubt_the_agent_has_not_spoken_to_is_an_ask(session):
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    await writer.submit(RequestCheck(subject=Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id),
                                    note="claims the queue is single-threaded"), Origin.BROWSER)
    asks = (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"]
    assert [a["kind"] for a in asks] == ["check"]
    assert asks[0]["subject"]["id"] == highlight.id


async def test_the_agent_speaking_on_the_subject_closes_the_doubt(session):
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)
    await writer.submit(RequestCheck(subject=subject), Origin.BROWSER)
    await writer.submit(PostMessage(anchor=subject, body="checked: it is not"), Origin.AGENT)
    assert (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"] == []


async def test_the_reviewers_own_words_do_not_close_their_doubt(session):
    """Otherwise asking and then adding a detail would answer the ask with the ask."""
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)
    await writer.submit(RequestCheck(subject=subject), Origin.BROWSER)
    await writer.submit(PostMessage(anchor=subject, body="specifically the retry"), Origin.BROWSER)
    kinds = [a["kind"] for a in (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"]]
    assert "check" in kinds


async def test_a_doubt_about_another_subject_stays_open(session):
    manager, sid = session
    writer = manager.get(sid)
    one, two = await mark(writer, line=1), await mark(writer, line=9)
    await writer.submit(RequestCheck(subject=Subject(kind=SubjectKind.HIGHLIGHT, id=one.id)),
                       Origin.BROWSER)
    await writer.submit(PostMessage(anchor=Subject(kind=SubjectKind.HIGHLIGHT, id=two.id),
                                   body="unrelated"), Origin.AGENT)
    assert [a["kind"] for a in
            (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"]] == ["check"]


async def test_doubting_what_the_agent_already_said_is_not_born_answered(session):
    """The central case: what raises the doubt is the agent having spoken. Only later words count."""
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)
    await writer.submit(PostMessage(anchor=subject, body="the queue is single-threaded"), Origin.AGENT)
    await writer.submit(RequestCheck(subject=subject, note="the queue is single-threaded"),
                       Origin.BROWSER)
    assert [a["kind"] for a in
            (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"]] == ["check"]


async def test_a_word_on_the_subject_closes_the_doubt_whatever_it_answered(session):
    """The surprise in docs/surprising-behaviors.md, pinned so a future closing verb is a deliberate change."""
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)
    await writer.submit(RequestCheck(subject=subject, note="doubt this"), Origin.BROWSER)
    await writer.submit(PostMessage(anchor=subject, body="it is defined in utils.py"), Origin.AGENT)
    assert (await chat_for(manager, ATTACHED).build(sid))["agent"]["asks"] == []


async def test_a_doubt_shows_on_the_conversation_it_will_be_answered_in(session):
    """Waiting on a check is waiting on a message, so it reads where that message will appear."""
    manager, sid = session
    writer = manager.get(sid)
    highlight = await mark(writer)
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=highlight.id)
    await writer.submit(RequestCheck(subject=subject), Origin.BROWSER)

    chat = chat_for(manager, ATTACHED)
    row = rows(await chat.build(sid))[("highlight", highlight.id)]
    assert row["checking"] and row["count"] == 0, "a doubt with nothing said yet is still visible"
    assert (await chat.build(f"{sid}:highlight:{highlight.id}"))["checking"]
    assert not rows(await chat.build(sid))[("review", "")]["checking"]

    await writer.submit(PostMessage(anchor=subject, body="checked"), Origin.AGENT)
    assert not rows(await chat.build(sid))[("highlight", highlight.id)]["checking"]


def test_an_old_ask_with_an_agent_attached_is_flagged_stale():
    """An agent is there and the ask has sat: "being worked on" stops being the likely story."""
    from review_mate.view.asks import Ask
    old = (datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER + 60)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    assert agent_state([Ask(kind="chat", since=old)], ATTACHED).stale is True
    assert agent_state([Ask(kind="chat", since=fresh)], ATTACHED).stale is False
    assert agent_state([Ask(kind="chat", since=old)], ALONE).stale is False


def test_an_agents_own_question_is_not_the_reviewers_debt():
    """Nothing tells a question from a statement in a body, so the agent speaking last owes nothing
    and is owed nothing — inventing the distinction would report silence as work."""
    from review_mate.session.state import ChatMessage, SessionState
    state = SessionState(id="s", created_at="t", messages=[
        ChatMessage(id="m1", role="user", body="why?", created_at="2026-01-01T00:00:00+00:00"),
        ChatMessage(id="m2", role="agent", body="which case did you mean?",
                    created_at="2026-01-01T00:01:00+00:00")])
    assert outstanding(state) == []


# --- over the transport ------------------------------------------------------


def test_a_message_republishes_its_own_conversation_and_the_index(tmp_path):
    manager = SessionManager(root=tmp_path / "sessions", mr_source=HostStub())
    app = create_app(manager=manager, provider=HostStub(), with_mcp=False,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))
    with TestClient(app) as client:
        sid = client.post("/api/cmd", json={"cmd": "session.open",
                                            "args": {"ref": "g/p!1"}}).json()["session"]
        with client.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe",
                          "topics": [f"chat:{sid}", f"chat:{sid}:review"]})
            for _ in range(2):
                json.loads(ws.receive_text())
            client.post(f"/api/sessions/{sid}/commands",
                        json={"type": "post_message", "body": "what is this for?"})
            seen = {}
            for _ in range(2):
                msg = json.loads(ws.receive_text())
                seen[msg["topic"]] = msg["view"]
            assert set(seen) == {f"chat:{sid}", f"chat:{sid}:review"}
            assert seen[f"chat:{sid}:review"]["messages"][0]["body"] == "what is this for?"
            assert seen[f"chat:{sid}"]["agent"]["state"] in {"stalled", "working"}


def test_presence_reaches_a_client_with_no_event_behind_it(tmp_path, monkeypatch):
    """`attached` lapses by clock. Nothing in the session changes when a watcher walks away, so a
    view that said "Claude is working" would keep saying it until something unrelated happened."""
    import review_mate.server.app as app_module
    monkeypatch.setattr(app_module, "PRESENCE_TICK", 0.05)
    manager = SessionManager(root=tmp_path / "sessions", mr_source=HostStub())
    app = create_app(manager=manager, provider=HostStub(), with_mcp=False,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))
    broker = manager._activity_broker
    with TestClient(app) as client:
        sid = client.post("/api/cmd", json={"cmd": "session.open",
                                            "args": {"ref": "g/p!1"}}).json()["session"]
        client.post(f"/api/sessions/{sid}/commands",
                    json={"type": "post_message", "body": "look?"})
        with client.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"chat:{sid}"]})
            first = json.loads(ws.receive_text())["view"]
            assert first["agent"]["state"] == "stalled"      # asked, and nobody is listening

            broker._last_wait_at = datetime.now(timezone.utc)   # an agent starts long-polling
            arrived = next_frame(ws)                            # ... on the ticker, not an event
            assert arrived is not None, "presence never reached the client"
            assert arrived["view"]["agent"]["state"] == "working"
            assert arrived["view"]["agent"]["attached"] is True


def test_the_ticker_stops_with_the_last_watcher(tmp_path, monkeypatch):
    """Work that only makes sense while someone is looking starts and stops with the watching."""
    import review_mate.server.app as app_module
    monkeypatch.setattr(app_module, "PRESENCE_TICK", 0.05)
    manager = SessionManager(root=tmp_path / "sessions", mr_source=HostStub())
    app = create_app(manager=manager, provider=HostStub(), with_mcp=False,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="g/p", iid=1))
    with TestClient(app) as client:
        sid = client.post("/api/cmd", json={"cmd": "session.open",
                                            "args": {"ref": "g/p!1"}}).json()["session"]
        with client.websocket_connect("/api/stream") as ws:
            ws.send_json({"action": "subscribe", "topics": [f"chat:{sid}"]})
            json.loads(ws.receive_text())
            assert app.state.presence_running() is True
            ws.send_json({"action": "unsubscribe", "topics": [f"chat:{sid}"]})
            deadline = time.time() + 2
            while app.state.presence_running() and time.time() < deadline:
                time.sleep(0.05)
            assert app.state.presence_running() is False
