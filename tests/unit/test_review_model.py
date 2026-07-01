"""Exhaustive coverage of the review model: authority matrix, reducer branches, event round-trip."""
import pytest

from review_mate.session import events as ev
from review_mate.session import commands as cmd
from review_mate.session.commands import handle, Rejection, AUTHORITY
from review_mate.session.reducer import reduce, fold
from review_mate.session.state import (
    SessionState, Origin, Side, LineRange, MRMetadata, FileEntry, ChangeType,
    Highlight, Card, AccessRequest, ReviewThread, CardStatus, AccessStatus, ChatMessage,
    DraftComment, DraftStatus,
)

ALL_ORIGINS = [Origin.BROWSER, Origin.AGENT, Origin.SYSTEM]


def _state():
    return SessionState(id="s", created_at="t")


def _sample(cmd_type: str):
    return {
        "add_highlight": cmd.AddHighlight(file="a.py", side=Side.NEW, line_range=LineRange(start=1, end=1)),
        "remove_highlight": cmd.RemoveHighlight(highlight_id="x"),
        "request_context": cmd.RequestContext(highlight_id="x"),
        "decide_access": cmd.DecideAccess(request_id="x", approve=True),
        "end_session": cmd.EndSession(),
        "emit_card": cmd.EmitCard(highlight_id="x", body="b"),
        "update_card": cmd.UpdateCard(card_id="x"),
        "remove_card": cmd.RemoveCard(card_id="x"),
        "request_access": cmd.RequestAccess(repo="r", reason="why"),
        "apply_mr_metadata": cmd.ApplyMRMetadata(mr=MRMetadata(host="h", project="p", iid=1, title="t",
                              source_branch="x", target_branch="m", sha="s", author="a", url="u")),
        "apply_files": cmd.ApplyFiles(files=[]),
        "apply_thread": cmd.ApplyThread(thread=ReviewThread(id="t")),
        "post_message": cmd.PostMessage(body="hi"),
        "clear_chat": cmd.ClearChat(),
        "save_draft": cmd.SaveDraft(highlight_id="x", body="b"),
        "remove_draft": cmd.RemoveDraft(highlight_id="x"),
        "mark_draft_posted": cmd.MarkDraftPosted(highlight_id="x"),
    }[cmd_type]


# --- AC-9: the full write-authority matrix (negative cells) ------------------

@pytest.mark.parametrize("cmd_type, allowed", sorted((k, tuple(v)) for k, v in AUTHORITY.items()))
def test_authority_rejects_every_disallowed_origin(cmd_type, allowed):
    command = _sample(cmd_type)
    for origin in ALL_ORIGINS:
        result = handle(_state(), command, origin)
        if origin in allowed:
            continue  # positive cells need referential setup; covered elsewhere
        assert isinstance(result, Rejection)
        assert "may not" in result.reason


def test_authority_positive_cells_that_need_no_setup():
    s = _state()
    assert isinstance(handle(s, _sample("add_highlight"), Origin.BROWSER), list)
    assert isinstance(handle(s, _sample("end_session"), Origin.BROWSER), list)
    assert isinstance(handle(s, _sample("request_access"), Origin.AGENT), list)
    for t in ("apply_mr_metadata", "apply_files", "apply_thread"):
        assert isinstance(handle(s, _sample(t), Origin.SYSTEM), list)


def test_decide_access_already_decided_is_rejected():
    s = _state()
    s = fold(s, handle(s, _sample("request_access"), Origin.AGENT))
    rid = s.access_requests[0].id
    s = fold(s, handle(s, cmd.DecideAccess(request_id=rid, approve=True), Origin.BROWSER))
    again = handle(s, cmd.DecideAccess(request_id=rid, approve=False), Origin.BROWSER)
    assert isinstance(again, Rejection) and "already decided" in again.reason


# --- reducer branches not otherwise exercised -------------------------------

def test_card_updated_partial_fields():
    s = _state()
    s = reduce(s, ev.CardEmitted(seq=1, ts="t", origin=Origin.AGENT,
               card=Card(id="c", highlight_id="h", body="orig", status=CardStatus.STREAMING)))
    s = reduce(s, ev.CardUpdated(seq=2, ts="t", origin=Origin.AGENT, card_id="c", body="new"))
    assert s.cards[0].body == "new" and s.cards[0].status is CardStatus.STREAMING  # status untouched
    s = reduce(s, ev.CardUpdated(seq=3, ts="t", origin=Origin.AGENT, card_id="c",
               status=CardStatus.COMPLETE, citations=["spec#1"]))
    assert s.cards[0].status is CardStatus.COMPLETE and s.cards[0].citations == ["spec#1"]
    assert s.cards[0].body == "new"  # body preserved


def test_thread_applied_replaces_in_place():
    s = _state()
    s = reduce(s, ev.ThreadApplied(seq=1, ts="t", origin=Origin.SYSTEM,
               thread=ReviewThread(id="t1", resolved=False)))
    s = reduce(s, ev.ThreadApplied(seq=2, ts="t", origin=Origin.SYSTEM,
               thread=ReviewThread(id="t1", resolved=True)))
    assert len(s.threads) == 1 and s.threads[0].resolved is True  # upsert, not duplicate


def test_card_removed_and_chat_cleared_reduce():
    s = _state()
    s = reduce(s, ev.CardEmitted(seq=1, ts="t", origin=Origin.AGENT,
               card=Card(id="c", body="insight")))  # MR-level card (no anchor)
    s = reduce(s, ev.CardRemoved(seq=2, ts="t", origin=Origin.BROWSER, card_id="c"))
    assert s.cards == []
    s = reduce(s, ev.MessagePosted(seq=3, ts="t", origin=Origin.BROWSER,
               message=ChatMessage(id="m", role="user", body="hi")))
    s = reduce(s, ev.ChatCleared(seq=4, ts="t", origin=Origin.BROWSER))
    assert s.messages == []


def test_draft_lifecycle_save_upsert_post_and_orphan_cleanup():
    s = _state()
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=1, end=1)), Origin.BROWSER))
    hid = s.highlights[0].id
    # save then update keeps one draft per highlight (upsert by highlight_id, id stable)
    s = fold(s, handle(s, cmd.SaveDraft(highlight_id=hid, body="first"), Origin.BROWSER))
    did = s.drafts[0].id
    s = fold(s, handle(s, cmd.SaveDraft(highlight_id=hid, body="second"), Origin.BROWSER))
    assert len(s.drafts) == 1 and s.drafts[0].body == "second" and s.drafts[0].id == did
    # mark posted carries the url and flips status
    s = fold(s, handle(s, cmd.MarkDraftPosted(highlight_id=hid, url="u#note_9"), Origin.BROWSER))
    assert s.drafts[0].status is DraftStatus.POSTED and s.drafts[0].url == "u#note_9"
    # dismissing the highlight drops its draft (no orphans)
    s = fold(s, handle(s, cmd.RemoveHighlight(highlight_id=hid), Origin.BROWSER))
    assert s.drafts == []


def test_save_draft_for_unknown_highlight_rejected():
    s = _state()
    assert isinstance(handle(s, cmd.SaveDraft(highlight_id="nope", body="b"), Origin.BROWSER),
                      Rejection)


def test_drafts_are_browser_only():
    s = _state()
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=1, end=1)), Origin.BROWSER))
    hid = s.highlights[0].id
    assert isinstance(handle(s, cmd.SaveDraft(highlight_id=hid, body="b"), Origin.AGENT), Rejection)


def test_request_context_sets_the_escalation_flag():
    s = _state()
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=1, end=1)), Origin.BROWSER))
    hid = s.highlights[0].id
    assert s.highlights[0].context_requested is False           # bare highlight: cheap tier only
    s = fold(s, handle(s, cmd.RequestContext(highlight_id=hid, question="why?"), Origin.BROWSER))
    assert s.highlights[0].context_requested is True and s.highlights[0].question == "why?"


def test_request_context_for_unknown_highlight_rejected():
    s = _state()
    assert isinstance(handle(s, cmd.RequestContext(highlight_id="nope"), Origin.BROWSER), Rejection)


def test_mr_and_files_reduce():
    s = _state()
    s = reduce(s, ev.MRMetadataApplied(seq=1, ts="t", origin=Origin.SYSTEM,
               mr=MRMetadata(host="h", project="p", iid=1, title="T", source_branch="x",
                             target_branch="m", sha="s", author="a", url="u")))
    s = reduce(s, ev.FilesApplied(seq=2, ts="t", origin=Origin.SYSTEM,
               files=[FileEntry(path="a.py", change_type=ChangeType.MODIFIED)]))
    assert s.mr.title == "T" and [f.path for f in s.files] == ["a.py"]


# --- every event type survives the JSONL round-trip (resume rests on this) ---

@pytest.mark.parametrize("event", [
    ev.SessionCreated(seq=1, ts="t", origin=Origin.SYSTEM),
    ev.MRMetadataApplied(seq=2, ts="t", origin=Origin.SYSTEM, mr=MRMetadata(host="h", project="p",
        iid=1, title="t", source_branch="x", target_branch="m", sha="s", author="a", url="u")),
    ev.FilesApplied(seq=3, ts="t", origin=Origin.SYSTEM, files=[FileEntry(path="a", change_type=ChangeType.ADDED)]),
    ev.HighlightAdded(seq=4, ts="t", origin=Origin.BROWSER, highlight=Highlight(id="h", file="a",
        side=Side.NEW, line_range=LineRange(start=1, end=1))),
    ev.HighlightRemoved(seq=5, ts="t", origin=Origin.BROWSER, highlight_id="h"),
    ev.CardEmitted(seq=6, ts="t", origin=Origin.AGENT, card=Card(id="c", highlight_id="h", body="b")),
    ev.CardUpdated(seq=7, ts="t", origin=Origin.AGENT, card_id="c", body="x"),
    ev.AccessRequested(seq=8, ts="t", origin=Origin.AGENT, request=AccessRequest(id="r", repo="x", reason="y")),
    ev.AccessDecided(seq=9, ts="t", origin=Origin.BROWSER, request_id="r", status=AccessStatus.APPROVED, decided_at="t"),
    ev.ThreadApplied(seq=10, ts="t", origin=Origin.SYSTEM, thread=ReviewThread(id="t")),
    ev.SessionEnded(seq=11, ts="t", origin=Origin.BROWSER),
    ev.CardRemoved(seq=12, ts="t", origin=Origin.BROWSER, card_id="c"),
    ev.ChatCleared(seq=13, ts="t", origin=Origin.BROWSER),
    ev.DraftSaved(seq=14, ts="t", origin=Origin.BROWSER,
        draft=DraftComment(id="d", highlight_id="h", body="nit")),
    ev.DraftRemoved(seq=15, ts="t", origin=Origin.BROWSER, highlight_id="h"),
    ev.DraftPosted(seq=16, ts="t", origin=Origin.BROWSER, highlight_id="h", url="u#note_1"),
])
def test_event_roundtrip_all_types(event):
    back = ev.parse_event(event.model_dump_json())
    assert type(back) is type(event) and back.seq == event.seq
