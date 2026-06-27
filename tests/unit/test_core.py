"""Unit tests for the pure core: state models, events, command authority, the reducer.

No IO. Covers AC-9 (authority partitioning), AC-13 (the contract document set), and the
reduce-fold correctness the event-sourced model rests on.
"""
import pytest

from review_mate.session.state import (
    SessionState, SessionStatus, Side, Origin, ChangeType,
    MRMetadata, FileEntry, LineRange, Highlight, Card, AccessRequest, ReviewThread,
)
from review_mate.session import events as ev
from review_mate.session import commands as cmd
from review_mate.session.commands import handle, Rejection
from review_mate.session.reducer import reduce, fold


def new_state() -> SessionState:
    return SessionState(id="s1", created_at="2026-06-27T00:00:00Z")


# --- AC-13: the contract document set ---------------------------------------

def test_session_state_has_exactly_the_six_document_types():
    s = new_state()
    assert s.mr is None
    for field in ("files", "highlights", "cards", "access_requests", "threads"):
        assert getattr(s, field) == []
    # the six contract collections + mr, nothing the design did not name
    doc_fields = {"mr", "files", "highlights", "cards", "access_requests", "threads"}
    envelope = {"id", "status", "created_at", "seq"}
    assert set(SessionState.model_fields) == doc_fields | envelope
    assert s.status is SessionStatus.ACTIVE


# --- AC-9: write-authority partitioning -------------------------------------

def test_browser_may_add_highlight_agent_may_not():
    s = new_state()
    c = cmd.AddHighlight(file="a.py", side=Side.NEW, line_range=LineRange(start=1, end=3))
    assert isinstance(handle(s, c, Origin.BROWSER), list)
    assert isinstance(handle(s, c, Origin.AGENT), Rejection)


def test_agent_may_emit_card_browser_may_not():
    s = new_state()
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=1, end=1)), Origin.BROWSER))
    hl_id = s.highlights[0].id
    c = cmd.EmitCard(highlight_id=hl_id, body="context")
    assert isinstance(handle(s, c, Origin.AGENT), list)
    assert isinstance(handle(s, c, Origin.BROWSER), Rejection)


def test_only_system_may_apply_mr_metadata():
    s = new_state()
    mr = MRMetadata(host="gitlab", project="g/p", iid=1, title="t", source_branch="x",
                    target_branch="main", sha="deadbeef", author="dev", url="http://x")
    c = cmd.ApplyMRMetadata(mr=mr)
    assert isinstance(handle(s, c, Origin.SYSTEM), list)
    assert isinstance(handle(s, c, Origin.BROWSER), Rejection)
    assert isinstance(handle(s, c, Origin.AGENT), Rejection)


def test_emit_card_for_unknown_highlight_is_rejected():
    s = new_state()
    r = handle(s, cmd.EmitCard(highlight_id="nope", body="x"), Origin.AGENT)
    assert isinstance(r, Rejection)


# --- reduce / fold correctness ----------------------------------------------

def test_reduce_applies_highlight_and_sets_seq():
    s = new_state()
    e = ev.HighlightAdded(seq=5, ts="t", origin=Origin.BROWSER,
                          highlight=Highlight(id="h1", file="a.py", side=Side.NEW,
                                              line_range=LineRange(start=1, end=2),
                                              author=Origin.BROWSER, created_at="t"))
    s2 = reduce(s, e)
    assert s2.seq == 5
    assert [h.id for h in s2.highlights] == ["h1"]
    assert s.highlights == []  # reduce is pure — original untouched


def test_full_command_lifecycle_through_fold():
    s = new_state()
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=10, end=12),
                                           question="why?"), Origin.BROWSER))
    hid = s.highlights[0].id
    s = fold(s, handle(s, cmd.EmitCard(highlight_id=hid, body="because"), Origin.AGENT))
    assert s.cards[0].highlight_id == hid
    s = fold(s, handle(s, cmd.RemoveHighlight(highlight_id=hid), Origin.BROWSER))
    assert s.highlights == []


def test_end_session_transitions_status():
    s = new_state()
    s = fold(s, handle(s, cmd.EndSession(), Origin.BROWSER))
    assert s.status is SessionStatus.ENDED


def test_access_request_then_decision():
    s = new_state()
    s = fold(s, handle(s, cmd.RequestAccess(repo="g/other", reason="contract"), Origin.AGENT))
    req_id = s.access_requests[0].id
    assert s.access_requests[0].status.value == "pending"
    s = fold(s, handle(s, cmd.DecideAccess(request_id=req_id, approve=True), Origin.BROWSER))
    assert s.access_requests[0].status.value == "approved"


# --- event (de)serialization round-trip (JSONL persistence rests on it) ------

def test_event_json_roundtrip_discriminated():
    e = ev.CardEmitted(seq=3, ts="t", origin=Origin.AGENT,
                       card=Card(id="c1", highlight_id="h1", body="b", created_at="t"))
    line = e.model_dump_json()
    back = ev.parse_event(line)
    assert isinstance(back, ev.CardEmitted)
    assert back.card.id == "c1"
    assert back.seq == 3
