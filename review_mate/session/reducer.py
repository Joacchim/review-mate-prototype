"""The pure fold: `state' = reduce(state, event)`. No IO, no mutation of the input.

This is the only place state evolves. Replaying a session's event log through `fold` rebuilds its
state exactly — the basis for durable resume (AC-8).
"""
from __future__ import annotations

from review_mate.session import events as ev
from review_mate.session.state import SessionState, SessionStatus


def reduce(state: SessionState, event: "ev.Event") -> SessionState:
    s = state.model_copy(deep=True)

    if isinstance(event, ev.SessionCreated):
        pass
    elif isinstance(event, ev.MRMetadataApplied):
        s.mr = event.mr
    elif isinstance(event, ev.FilesApplied):
        s.files = list(event.files)
    elif isinstance(event, ev.HighlightAdded):
        s.highlights.append(event.highlight)
    elif isinstance(event, ev.HighlightRemoved):
        s.highlights = [h for h in s.highlights if h.id != event.highlight_id]
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
    elif isinstance(event, ev.MessagePosted):
        s.messages.append(event.message)
    elif isinstance(event, ev.ChatCleared):
        s.messages = []
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
