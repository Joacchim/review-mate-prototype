"""Exhaustive coverage of the review model: authority matrix, reducer branches, event round-trip."""
import pytest

from review_mate.session import events as ev
from review_mate.session import commands as cmd
from review_mate.session.commands import handle, Rejection, AUTHORITY
from review_mate.session.reducer import reduce, fold
from review_mate.session.state import (
    SessionState, Origin, Side, LineRange, MRMetadata, FileEntry, ChangeType,
    Highlight, Card, AccessRequest, ReviewThread, CardStatus, AccessStatus, ChatMessage, Addressed,
    CheckRequest,
    Criticality, DraftComment, DraftStatus, Grant, Label, ReviewedFile, Subject, SubjectKind,
    Theme,
)

ALL_ORIGINS = [Origin.BROWSER, Origin.AGENT, Origin.SYSTEM]


def _state():
    return SessionState(id="s", created_at="t")


def _sample(cmd_type: str):
    return {
        "add_highlight": cmd.AddHighlight(file="a.py", side=Side.NEW, line_range=LineRange(start=1, end=1)),
        "remove_highlight": cmd.RemoveHighlight(highlight_id="x"),
        "mark_file_reviewed": cmd.MarkFileReviewed(path="a.py"),
        "unmark_file_reviewed": cmd.UnmarkFileReviewed(path="a.py"),
        "request_context": cmd.RequestContext(highlight_id="x"),
        "request_insights": cmd.RequestInsights(),
        "request_check": cmd.RequestCheck(
            subject=Subject(kind=SubjectKind.HIGHLIGHT, id="x")),
        "decide_access": cmd.DecideAccess(request_id="x", approve=True),
        "end_session": cmd.EndSession(),
        "emit_card": cmd.EmitCard(highlight_id="x", body="b"),
        "update_card": cmd.UpdateCard(card_id="x"),
        "remove_card": cmd.RemoveCard(card_id="x"),
        "request_access": cmd.RequestAccess(repo="r", reason="why"),
        "apply_mr_metadata": cmd.ApplyMRMetadata(mr=MRMetadata(host="h", project="p", iid=1, title="t",
                              source_branch="x", target_branch="m", sha="s", author="a", url="u")),
        "set_checkout": cmd.SetCheckout(path="/tmp/checkout"),
        "apply_files": cmd.ApplyFiles(files=[]),
        "apply_thread": cmd.ApplyThread(thread=ReviewThread(id="t")),
        "replace_threads": cmd.ReplaceThreads(threads=[]),
        "post_message": cmd.PostMessage(body="hi"),
        "clear_chat": cmd.ClearChat(),
        "save_draft": cmd.SaveDraft(highlight_id="x", body="b"),
        "remove_draft": cmd.RemoveDraft(highlight_id="x"),
        "mark_draft_posted": cmd.MarkDraftPosted(highlight_id="x"),
        "record_grant": cmd.RecordGrant(request_id="x", grant=Grant()),
        "record_addressed": cmd.RecordAddressed(
            subject=Subject(kind=SubjectKind.HIGHLIGHT, id="x"), sha="abc"),
        "label_card": cmd.LabelCard(card_id="x", label=Label(
            theme=Theme.BUG, criticality=Criticality.HIGH)),
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
    assert s.highlights[0].context_requested is False           # bare highlight: host context only
    s = fold(s, handle(s, cmd.RequestContext(highlight_id=hid, question="why?"), Origin.BROWSER))
    assert s.highlights[0].context_requested is True and s.highlights[0].question == "why?"
    # stamped with the escalation time, so the UI can say how long Claude has been on it
    assert s.highlights[0].context_requested_at


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
    ev.CheckoutSet(seq=17, ts="t", origin=Origin.SYSTEM, path="/tmp/wt"),
    ev.ContextRequested(seq=18, ts="t", origin=Origin.BROWSER, highlight_id="h", question="why?"),
    ev.CardLabelled(seq=19, ts="t", origin=Origin.AGENT, card_id="c",
        label=Label(theme=Theme.BUG, criticality=Criticality.HIGH, by=Origin.AGENT)),
    ev.SubjectAddressed(seq=20, ts="t", origin=Origin.AGENT, record=Addressed(
        subject=Subject(kind=SubjectKind.HIGHLIGHT, id="h"), sha="abc", summary="fixed")),
    ev.AccessGrantChanged(seq=21, ts="t", origin=Origin.SYSTEM, request_id="r",
        grant=Grant(repo="x", path="/tmp/x")),
    ev.ThreadsReplaced(seq=22, ts="t", origin=Origin.SYSTEM, threads=[ReviewThread(id="t")]),
    ev.InsightsRequested(seq=23, ts="t", origin=Origin.BROWSER, sha="abc"),
    ev.CheckRequested(seq=24, ts="t", origin=Origin.BROWSER, request=CheckRequest(
        id="k", subject=Subject(kind=SubjectKind.HIGHLIGHT, id="h"))),
    ev.MessagePosted(seq=25, ts="t", origin=Origin.BROWSER, message=ChatMessage(
        id="m", role=Origin.BROWSER, body="hello")),
    ev.FileReviewed(seq=26, ts="t", origin=Origin.BROWSER,
        file=ReviewedFile(path="a.py", sha="abc", fingerprint="f", at="t")),
    ev.FileUnreviewed(seq=27, ts="t", origin=Origin.BROWSER, path="a.py"),
])
def test_event_roundtrip_all_types(event):
    back = ev.parse_event(event.model_dump_json())
    assert type(back) is type(event) and back.seq == event.seq


def test_the_roundtrip_list_covers_every_event_there_is():
    """The list above is written by hand, and it had fallen nine events behind.

    A persisted event missing from it is a resume bug nothing else catches: the suite stays green
    while the event that never round-tripped is the one that fails to come back. Checking the list
    against the union is what stops it drifting again.
    """
    from typing import get_args
    declared = {cls.__name__ for cls in get_args(get_args(ev.Event)[0])}
    covered = {type(case).__name__
               for case in test_event_roundtrip_all_types.pytestmark[0].args[1]}
    assert declared == covered, f"never round-tripped: {sorted(declared - covered)}"


# --- a review pass remembers which code it was about --------------------------

def _with_mr(sha="abc123"):
    s = _state()
    return fold(s, handle(s, cmd.ApplyMRMetadata(mr=MRMetadata(
        host="h", project="p", iid=1, title="t", source_branch="x", target_branch="m",
        sha=sha, author="a", url="u")), Origin.SYSTEM))


def test_a_review_pass_records_the_head_it_was_asked_about():
    """So a pass the change has moved past reads as being about an earlier version, rather than
    disappearing and leaving the reviewer watching a cue that silently stopped."""
    s = _with_mr(sha="abc123")
    s = fold(s, handle(s, cmd.RequestInsights(), Origin.BROWSER))
    assert s.insights_requested is True and s.insights_requested_sha == "abc123"


def test_a_pass_asked_for_with_no_mr_loaded_records_no_head():
    s = _state()
    s = fold(s, handle(s, cmd.RequestInsights(), Origin.BROWSER))
    assert s.insights_requested is True and s.insights_requested_sha is None


def test_a_head_that_moves_leaves_the_pass_pointing_at_the_old_one():
    """It is not cleared: what was asked about is a fact, and losing it is what made the waiting
    cue vanish with nothing to explain it."""
    s = _with_mr(sha="abc123")
    s = fold(s, handle(s, cmd.RequestInsights(), Origin.BROWSER))
    s = fold(s, handle(s, cmd.ApplyMRMetadata(mr=s.mr.model_copy(update={"sha": "def456"})),
                       Origin.SYSTEM))
    assert s.insights_requested is True and s.insights_requested_sha == "abc123"


def test_asking_again_restamps_the_head():
    """Asking about the new code is a new pass, not a repeat of the stale one."""
    s = _with_mr(sha="abc123")
    s = fold(s, handle(s, cmd.RequestInsights(), Origin.BROWSER))
    s = fold(s, handle(s, cmd.ApplyMRMetadata(mr=s.mr.model_copy(update={"sha": "def456"})),
                       Origin.SYSTEM))
    s = fold(s, handle(s, cmd.RequestInsights(), Origin.BROWSER))
    assert s.insights_requested_sha == "def456"


# --- asking for something to be double-checked --------------------------------

def _with_highlight():
    s = _with_mr()
    return fold(s, handle(s, _sample("add_highlight"), Origin.BROWSER))


def test_a_check_records_what_to_verify_and_which_code():
    s = _with_highlight()
    hid = s.highlights[0].id
    s = fold(s, handle(s, cmd.RequestCheck(
        subject=Subject(kind=SubjectKind.HIGHLIGHT, id=hid),
        note="this claims the queue is single-threaded"), Origin.BROWSER))

    check = s.checks[0]
    assert check.subject.id == hid and check.subject.kind is SubjectKind.HIGHLIGHT
    assert check.note == "this claims the queue is single-threaded"
    assert check.sha == "abc123"      # which code it was about, as a pass and a highlight both do


def test_a_check_on_something_that_is_not_there_is_rejected():
    """The same guard a message anchored to nothing gets: an ask nothing can open is unreachable."""
    s = _with_mr()
    out = handle(s, cmd.RequestCheck(subject=Subject(kind=SubjectKind.HIGHLIGHT, id="nope")),
                 Origin.BROWSER)
    assert isinstance(out, Rejection) and "no such highlight" in out.reason


def test_a_check_can_be_asked_about_the_agents_own_insight():
    s = _with_mr()
    s = fold(s, handle(s, cmd.EmitCard(highlight_id=None, body="the reducer is the only writer"),
                       Origin.AGENT))
    cid = s.cards[0].id
    s = fold(s, handle(s, cmd.RequestCheck(subject=Subject(kind=SubjectKind.INSIGHT, id=cid)),
                       Origin.BROWSER))
    assert s.checks[0].subject.kind is SubjectKind.INSIGHT and s.checks[0].note == ""


def test_several_checks_stand_on_their_own():
    s = _with_highlight()
    hid = s.highlights[0].id
    for note in ("first doubt", "second doubt"):
        s = fold(s, handle(s, cmd.RequestCheck(
            subject=Subject(kind=SubjectKind.HIGHLIGHT, id=hid), note=note), Origin.BROWSER))
    assert [c.note for c in s.checks] == ["first doubt", "second doubt"]


# --- what an approval produces -------------------------------------------------

def _with_request(approve=None):
    s = _with_mr()
    s = fold(s, handle(s, cmd.RequestAccess(repo="g/sibling", reason="the caller lives there"),
                       Origin.AGENT))
    if approve is not None:
        rid = s.access_requests[0].id
        s = fold(s, handle(s, cmd.DecideAccess(request_id=rid, approve=approve), Origin.BROWSER))
    return s


def test_an_approval_alone_produces_nothing():
    """Deciding is the reviewer's move; materializing is the server's, and may not have started."""
    s = _with_request(approve=True)
    assert s.access_requests[0].status is AccessStatus.APPROVED
    assert s.access_requests[0].grant is None


def test_a_grant_records_where_the_repository_landed():
    s = _with_request(approve=True)
    rid = s.access_requests[0].id
    s = fold(s, handle(s, cmd.RecordGrant(request_id=rid, grant=Grant(state="materializing")),
                       Origin.SYSTEM))
    assert s.access_requests[0].grant.state == "materializing"

    s = fold(s, handle(s, cmd.RecordGrant(
        request_id=rid, grant=Grant(state="ready", path="/tmp/x")), Origin.SYSTEM))
    grant = s.access_requests[0].grant
    assert grant.state == "ready" and grant.path == "/tmp/x"


def test_a_failed_materialization_is_recorded_rather_than_dropped():
    """Otherwise an approval that could not be honoured looks exactly like one still working."""
    s = _with_request(approve=True)
    rid = s.access_requests[0].id
    s = fold(s, handle(s, cmd.RecordGrant(
        request_id=rid, grant=Grant(state="failed", error="no such project")), Origin.SYSTEM))
    assert s.access_requests[0].grant.state == "failed"
    assert "no such project" in s.access_requests[0].grant.error


def test_nothing_materializes_for_a_request_the_reviewer_refused():
    """The consent invariant, enforced where a command is checked rather than where one is sent."""
    s = _with_request(approve=False)
    out = handle(s, cmd.RecordGrant(request_id=s.access_requests[0].id,
                                    grant=Grant(state="ready", path="/tmp/x")), Origin.SYSTEM)
    assert isinstance(out, Rejection) and "not approved" in out.reason


def test_nothing_materializes_for_a_request_nobody_has_answered():
    s = _with_request()
    out = handle(s, cmd.RecordGrant(request_id=s.access_requests[0].id, grant=Grant()),
                 Origin.SYSTEM)
    assert isinstance(out, Rejection) and "not approved" in out.reason


def test_a_grant_for_no_such_request_is_rejected():
    s = _with_mr()
    out = handle(s, cmd.RecordGrant(request_id="nope", grant=Grant()), Origin.SYSTEM)
    assert isinstance(out, Rejection) and "no such access request" in out.reason


# --- what an insight is about, and how much it matters -------------------------

def _labelled(theme=Theme.BUG, crit=Criticality.HIGH, about="the retry path"):
    return Label(theme=theme, criticality=crit, about=about)


def test_an_insight_can_say_what_it_is_and_how_much_it_matters():
    s = _with_mr()
    s = fold(s, handle(s, cmd.EmitCard(highlight_id=None, body="the retry is unbounded",
                                       label=_labelled()), Origin.AGENT))
    label = s.cards[0].label
    assert label.theme is Theme.BUG and label.criticality is Criticality.HIGH
    assert label.about == "the retry path"
    assert label.by is Origin.AGENT, "the agent's own claim"


def test_an_unlabelled_insight_stays_unlabelled():
    """Never inferred. A card with no label is a card nobody classified, and says so."""
    s = _with_mr()
    s = fold(s, handle(s, cmd.EmitCard(highlight_id=None, body="a note"), Origin.AGENT))
    assert s.cards[0].label is None


def test_the_reviewer_can_correct_a_claim_they_disagree_with():
    """A finding labelled wrongly is still a finding — correcting beats dismissing it."""
    s = _with_mr()
    s = fold(s, handle(s, cmd.EmitCard(highlight_id=None, body="x", label=_labelled()),
                       Origin.AGENT))
    cid = s.cards[0].id
    s = fold(s, handle(s, cmd.LabelCard(card_id=cid, label=_labelled(
        theme=Theme.STYLE, crit=Criticality.LOW, about="")), Origin.BROWSER))
    label = s.cards[0].label
    assert label.theme is Theme.STYLE and label.criticality is Criticality.LOW
    assert label.by is Origin.BROWSER, "whose claim it now is, so a client can say so"


def test_who_labelled_it_is_never_the_callers_to_say():
    """Same rule a chat message's role follows: the origin decides, not the payload."""
    s = _with_mr()
    s = fold(s, handle(s, cmd.EmitCard(highlight_id=None, body="x"), Origin.AGENT))
    cid = s.cards[0].id
    forged = Label(theme=Theme.BUG, criticality=Criticality.HIGH, by=Origin.BROWSER)
    s = fold(s, handle(s, cmd.LabelCard(card_id=cid, label=forged), Origin.AGENT))
    assert s.cards[0].label.by is Origin.AGENT


def test_labelling_something_that_is_not_there_is_rejected():
    s = _with_mr()
    out = handle(s, cmd.LabelCard(card_id="nope", label=_labelled()), Origin.AGENT)
    assert isinstance(out, Rejection) and "no such card" in out.reason


def test_a_theme_outside_the_vocabulary_is_not_a_command():
    """Closed on purpose: an open list is a filter row where `perf` and `performance` both appear
    and neither finds the other's cards."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        Label(theme="cleanliness", criticality=Criticality.LOW)


# --- the agent changing the code, rather than explaining it --------------------

def test_a_fix_is_recorded_against_what_it_answers():
    """Five open comments and one new commit is a matching exercise nobody should have to do."""
    s = _with_highlight()
    hid = s.highlights[0].id
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=hid)
    s = fold(s, handle(s, cmd.RecordAddressed(subject=subject, sha="def456",
                                              summary="bounded the retry at five"), Origin.AGENT))
    record = s.addressed[0]
    assert record.subject.id == hid and record.sha == "def456"
    assert record.summary == "bounded the retry at five"


def test_a_fix_needs_the_sha_the_code_became():
    """Without it there is nothing to tell a fixed subject from one whose lines merely moved."""
    s = _with_highlight()
    out = handle(s, cmd.RecordAddressed(
        subject=Subject(kind=SubjectKind.HIGHLIGHT, id=s.highlights[0].id), sha=""), Origin.AGENT)
    assert isinstance(out, Rejection) and "sha" in out.reason


def test_a_fix_for_something_that_is_not_there_is_rejected():
    s = _with_mr()
    out = handle(s, cmd.RecordAddressed(
        subject=Subject(kind=SubjectKind.HIGHLIGHT, id="nope"), sha="abc"), Origin.AGENT)
    assert isinstance(out, Rejection) and "no such highlight" in out.reason


def test_the_reviewer_does_not_report_a_fix_on_the_agents_behalf():
    """A claim that something was fixed is worth exactly as much as who made it."""
    s = _with_highlight()
    out = handle(s, cmd.RecordAddressed(
        subject=Subject(kind=SubjectKind.HIGHLIGHT, id=s.highlights[0].id), sha="abc"),
        Origin.BROWSER)
    assert isinstance(out, Rejection) and "may not issue" in out.reason


def test_a_subject_can_be_addressed_more_than_once():
    """A fix that did not land the first time is answered again, and both attempts are the record."""
    s = _with_highlight()
    subject = Subject(kind=SubjectKind.HIGHLIGHT, id=s.highlights[0].id)
    for sha in ("aaa111", "bbb222"):
        s = fold(s, handle(s, cmd.RecordAddressed(subject=subject, sha=sha), Origin.AGENT))
    assert [r.sha for r in s.addressed] == ["aaa111", "bbb222"]


# --- a mark aimed at one commit rather than at the change ---------------------

def test_a_mark_made_while_reading_a_commit_records_which_one():
    s = _with_mr(sha="head9")
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=4, end=4),
                                           commit_sha="older1"), Origin.BROWSER))
    hl = s.highlights[0]
    assert hl.commit_sha == "older1"      # which code the lines are
    assert hl.created_sha == "head9"      # ... and which head was current, still separately


def test_marking_the_tip_records_no_commit_because_the_tip_is_the_head():
    """`commit_sha` means "a commit that is not the head". Recording the head there would make
    every ordinary mark look like one aimed at an earlier version."""
    s = _with_mr(sha="head9")
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=4, end=4),
                                           commit_sha="head9"), Origin.BROWSER))
    assert s.highlights[0].commit_sha is None


def test_an_ordinary_mark_names_no_commit():
    s = _with_mr(sha="head9")
    s = fold(s, handle(s, cmd.AddHighlight(file="a.py", side=Side.NEW,
                                           line_range=LineRange(start=4, end=4)), Origin.BROWSER))
    assert s.highlights[0].commit_sha is None
