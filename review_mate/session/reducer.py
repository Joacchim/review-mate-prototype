"""The pure fold: `state' = reduce(state, event)`. No IO, no mutation of the input.

This is the only place state evolves. Replaying a session's event log through `fold` rebuilds its
state exactly — the basis for durable resume (AC-8).
"""
from __future__ import annotations

from review_mate.session import events as ev
from review_mate.session.state import DraftStatus, SessionState, SessionStatus, SubjectKind


def reduce(state: SessionState, event: "ev.Event") -> SessionState:
    s = state.model_copy(deep=True)

    if isinstance(event, ev.SessionCreated):
        pass
    elif isinstance(event, ev.MRMetadataApplied):
        s.mr = event.mr
    elif isinstance(event, ev.CheckoutSet):
        s.checkout_path = event.path
    elif isinstance(event, ev.FilesApplied):
        s.files = list(event.files)
    elif isinstance(event, ev.HighlightAdded):
        # a log written before highlights were numbered carries ordinal 0: number it on replay, in
        # the order it was added, which is the numbering it had at the time
        highlight = event.highlight
        if not highlight.ordinal:
            highlight = highlight.model_copy(update={"ordinal": s.highlights_created + 1})
        s.highlights.append(highlight)
        s.highlights_created = max(s.highlights_created, highlight.ordinal)
    elif isinstance(event, ev.HighlightRemoved):
        s.highlights = [h for h in s.highlights if h.id != event.highlight_id]
        s.drafts = [d for d in s.drafts if d.highlight_id != event.highlight_id]  # no orphan drafts
        s.messages = [m for m in s.messages
                      if not (m.anchor is not None and m.anchor.kind is SubjectKind.HIGHLIGHT
                              and m.anchor.id == event.highlight_id)]
    elif isinstance(event, ev.ContextRequested):
        for h in s.highlights:
            if h.id == event.highlight_id:
                h.context_requested = True
                h.context_requested_at = event.ts
                if event.question:
                    h.question = event.question
    elif isinstance(event, ev.CardEmitted):
        s.cards.append(event.card)
    elif isinstance(event, ev.CardUpdated):
        for c in s.cards:
            if c.id == event.card_id:
                if event.body is not None:
                    c.body = event.body
                if event.status is not None:
                    c.status = event.status
                if event.citations is not None:
                    c.citations = list(event.citations)
    elif isinstance(event, ev.CardRemoved):
        s.cards = [c for c in s.cards if c.id != event.card_id]
        # dismissing an insight discards what was said about it, as removing a highlight discards
        # its draft: a conversation whose subject is gone has no row left to render it
        s.messages = [m for m in s.messages
                      if not (m.anchor is not None and m.anchor.kind is SubjectKind.INSIGHT
                              and m.anchor.id == event.card_id)]
    elif isinstance(event, ev.AccessRequested):
        s.access_requests.append(event.request)
    elif isinstance(event, ev.AccessDecided):
        for r in s.access_requests:
            if r.id == event.request_id:
                r.status = event.status
                r.decided_at = event.decided_at
    elif isinstance(event, ev.ThreadApplied):
        replaced = False
        for i, t in enumerate(s.threads):
            if t.id == event.thread.id:
                s.threads[i] = event.thread
                replaced = True
                break
        if not replaced:
            s.threads.append(event.thread)
    elif isinstance(event, ev.ThreadsReplaced):
        s.threads = list(event.threads)   # reconcile against the host's full current set (drops removed)
    elif isinstance(event, ev.MessagePosted):
        s.messages.append(event.message)
    elif isinstance(event, ev.ChatCleared):
        s.messages = [m for m in s.messages if m.anchor != event.anchor]
    elif isinstance(event, ev.InsightsRequested):
        s.insights_requested = True
        s.insights_requested_at = event.ts
    elif isinstance(event, ev.DraftSaved):
        replaced = False
        for i, d in enumerate(s.drafts):
            if d.highlight_id == event.draft.highlight_id:
                s.drafts[i] = event.draft
                replaced = True
                break
        if not replaced:
            s.drafts.append(event.draft)
    elif isinstance(event, ev.DraftRemoved):
        s.drafts = [d for d in s.drafts if d.highlight_id != event.highlight_id]
    elif isinstance(event, ev.DraftPosted):
        for d in s.drafts:
            if d.highlight_id == event.highlight_id:
                d.status = DraftStatus.POSTED
                d.url = event.url
                d.thread_id = event.thread_id
    elif isinstance(event, ev.SessionEnded):
        s.status = SessionStatus.ENDED
    else:  # pragma: no cover - exhaustive over the Event union
        raise TypeError(f"unknown event: {event!r}")

    s.seq = event.seq
    return s


def fold(state: SessionState, events: "list[ev.Event]") -> SessionState:
    """Apply a sequence of events left to right. Build's handle() returns such a list."""
    for event in events:
        state = reduce(state, event)
    return state
