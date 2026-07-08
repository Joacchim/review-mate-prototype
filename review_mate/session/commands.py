"""Commands — the only way to request a mutation — and `handle`, the pure command→events step.

`handle` enforces the **write-authority matrix** (the design's partitioning, AC-9 integrity): a
command whose `origin` lacks authority for its target document is rejected with no state change.
Authorized commands are validated and reduced to one or more events. `handle` may mint ids/timestamps
(it runs once per command); replay never re-runs it — it only re-runs the pure `reduce`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from review_mate.session import events as ev
from review_mate.session.state import (
    AccessRequest, AccessStatus, Card, CardStatus, ChatMessage, DraftComment, DraftStatus,
    FileEntry, Highlight, LineRange, MRMetadata, Origin, ReviewThread, Side,
)


# --- command types ----------------------------------------------------------

class AddHighlight(BaseModel):
    type: Literal["add_highlight"] = "add_highlight"
    file: str
    side: Side
    line_range: LineRange
    anchor: str | None = None
    question: str | None = None


class RemoveHighlight(BaseModel):
    type: Literal["remove_highlight"] = "remove_highlight"
    highlight_id: str


class RequestContext(BaseModel):
    """The reviewer escalates a highlight from the cheap tier to a full agent card (D21)."""
    type: Literal["request_context"] = "request_context"
    highlight_id: str
    question: str | None = None


class EmitCard(BaseModel):
    type: Literal["emit_card"] = "emit_card"
    highlight_id: str | None = None    # None = an MR-level insight, anchored to nothing
    body: str
    citations: list[str] = []


class RemoveCard(BaseModel):
    type: Literal["remove_card"] = "remove_card"
    card_id: str


class UpdateCard(BaseModel):
    type: Literal["update_card"] = "update_card"
    card_id: str
    body: str | None = None
    status: CardStatus | None = None
    citations: list[str] | None = None


class RequestAccess(BaseModel):
    type: Literal["request_access"] = "request_access"
    repo: str
    reason: str


class DecideAccess(BaseModel):
    type: Literal["decide_access"] = "decide_access"
    request_id: str
    approve: bool


class ApplyMRMetadata(BaseModel):
    type: Literal["apply_mr_metadata"] = "apply_mr_metadata"
    mr: MRMetadata


class SetCheckout(BaseModel):
    type: Literal["set_checkout"] = "set_checkout"
    path: str


class ApplyFiles(BaseModel):
    type: Literal["apply_files"] = "apply_files"
    files: list[FileEntry]


class ApplyThread(BaseModel):
    type: Literal["apply_thread"] = "apply_thread"
    thread: ReviewThread


class PostMessage(BaseModel):
    type: Literal["post_message"] = "post_message"
    body: str


class ClearChat(BaseModel):
    type: Literal["clear_chat"] = "clear_chat"


class SaveDraft(BaseModel):
    type: Literal["save_draft"] = "save_draft"
    highlight_id: str | None = None    # None = an MR-level review comment (a summary)
    body: str = ""
    suggestion: str | None = None      # optional suggested-change replacement lines (coexists with body)


class RemoveDraft(BaseModel):
    type: Literal["remove_draft"] = "remove_draft"
    highlight_id: str | None = None


class MarkDraftPosted(BaseModel):
    type: Literal["mark_draft_posted"] = "mark_draft_posted"
    highlight_id: str | None = None
    url: str | None = None
    thread_id: str | None = None       # the discussion the posted draft became (draft-as-thread)


class EndSession(BaseModel):
    type: Literal["end_session"] = "end_session"


Command = Union[
    AddHighlight, RemoveHighlight, RequestContext, EmitCard, UpdateCard, RemoveCard,
    RequestAccess, DecideAccess, ApplyMRMetadata, SetCheckout, ApplyFiles, ApplyThread,
    PostMessage, ClearChat, SaveDraft, RemoveDraft, MarkDraftPosted, EndSession,
]


_CommandUnion = Annotated[Command, Field(discriminator="type")]
_command_adapter: TypeAdapter = TypeAdapter(_CommandUnion)


def parse_command(data: dict) -> Command:
    """Decode a client-supplied command (a dict with a `type` tag) into its typed model."""
    return _command_adapter.validate_python(data)


class Rejection(BaseModel):
    reason: str


# --- the write-authority matrix (AC-9) --------------------------------------

AUTHORITY: dict[str, set[Origin]] = {
    # both may add a highlight: the reviewer flags what to explain; the agent flags what it found.
    # Highlight.author records which, and the single-writer property still holds (appends don't conflict).
    "add_highlight": {Origin.BROWSER, Origin.AGENT},
    "remove_highlight": {Origin.BROWSER},
    "request_context": {Origin.BROWSER},   # the reviewer escalates a highlight to the agent tier (D21)
    "remove_card": {Origin.BROWSER},   # the reviewer dismisses an insight card
    "decide_access": {Origin.BROWSER},
    "end_session": {Origin.BROWSER},
    "emit_card": {Origin.AGENT},
    "update_card": {Origin.AGENT},
    "request_access": {Origin.AGENT},
    "post_message": {Origin.BROWSER, Origin.AGENT},  # both sides of the chat
    "clear_chat": {Origin.BROWSER},
    # review drafts are the reviewer's own — browser only (the agent never posts in their name)
    "save_draft": {Origin.BROWSER},
    "remove_draft": {Origin.BROWSER},
    "mark_draft_posted": {Origin.BROWSER},
    "apply_mr_metadata": {Origin.SYSTEM},
    "set_checkout": {Origin.SYSTEM},
    "apply_files": {Origin.SYSTEM},
    "apply_thread": {Origin.SYSTEM},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def handle(state, command: Command, origin: Origin) -> "list[ev.Event] | Rejection":
    allowed = AUTHORITY.get(command.type)
    if allowed is None:
        return Rejection(reason=f"unknown command: {command.type}")
    if origin not in allowed:
        return Rejection(
            reason=f"{origin.value} may not issue {command.type}"
        )

    ts = _now()

    def emit(event_cls, **kw) -> list:
        return [event_cls(ts=ts, origin=origin, **kw)]

    if isinstance(command, AddHighlight):
        hl = Highlight(id=_id(), file=command.file, side=command.side,
                       line_range=command.line_range, anchor=command.anchor,
                       question=command.question, author=origin, created_at=ts,
                       created_sha=state.mr.sha if state.mr else None)
        return emit(ev.HighlightAdded, highlight=hl)

    if isinstance(command, RemoveHighlight):
        return emit(ev.HighlightRemoved, highlight_id=command.highlight_id)

    if isinstance(command, RequestContext):
        if not any(h.id == command.highlight_id for h in state.highlights):
            return Rejection(reason=f"no such highlight: {command.highlight_id}")
        return emit(ev.ContextRequested, highlight_id=command.highlight_id, question=command.question)

    if isinstance(command, EmitCard):
        if command.highlight_id is not None and \
                not any(h.id == command.highlight_id for h in state.highlights):
            return Rejection(reason=f"no such highlight: {command.highlight_id}")
        card = Card(id=_id(), highlight_id=command.highlight_id, body=command.body,
                    citations=command.citations, author=origin, created_at=ts)
        return emit(ev.CardEmitted, card=card)

    if isinstance(command, UpdateCard):
        if not any(c.id == command.card_id for c in state.cards):
            return Rejection(reason=f"no such card: {command.card_id}")
        return emit(ev.CardUpdated, card_id=command.card_id, body=command.body,
                    status=command.status, citations=command.citations)

    if isinstance(command, RemoveCard):
        if not any(c.id == command.card_id for c in state.cards):
            return Rejection(reason=f"no such card: {command.card_id}")
        return emit(ev.CardRemoved, card_id=command.card_id)

    if isinstance(command, RequestAccess):
        req = AccessRequest(id=_id(), repo=command.repo, reason=command.reason)
        return emit(ev.AccessRequested, request=req)

    if isinstance(command, DecideAccess):
        req = next((r for r in state.access_requests if r.id == command.request_id), None)
        if req is None:
            return Rejection(reason=f"no such access request: {command.request_id}")
        if req.status is not AccessStatus.PENDING:
            return Rejection(reason="access request already decided")
        status = AccessStatus.APPROVED if command.approve else AccessStatus.DENIED
        return emit(ev.AccessDecided, request_id=command.request_id, status=status, decided_at=ts)

    if isinstance(command, ApplyMRMetadata):
        return emit(ev.MRMetadataApplied, mr=command.mr)

    if isinstance(command, SetCheckout):
        return emit(ev.CheckoutSet, path=command.path)

    if isinstance(command, ApplyFiles):
        return emit(ev.FilesApplied, files=command.files)

    if isinstance(command, ApplyThread):
        return emit(ev.ThreadApplied, thread=command.thread)

    if isinstance(command, PostMessage):
        role = "user" if origin is Origin.BROWSER else "agent"
        msg = ChatMessage(id=_id(), role=role, body=command.body, created_at=ts)
        return emit(ev.MessagePosted, message=msg)

    if isinstance(command, SaveDraft):
        if command.highlight_id is not None and \
                not any(h.id == command.highlight_id for h in state.highlights):
            return Rejection(reason=f"no such highlight: {command.highlight_id}")
        existing = next((d for d in state.drafts if d.highlight_id == command.highlight_id), None)
        draft = DraftComment(id=existing.id if existing else _id(),
                             highlight_id=command.highlight_id, body=command.body,
                             suggestion=command.suggestion,
                             status=existing.status if existing else DraftStatus.DRAFT,
                             url=existing.url if existing else None,
                             thread_id=existing.thread_id if existing else None,
                             created_at=existing.created_at if existing else ts)
        return emit(ev.DraftSaved, draft=draft)

    if isinstance(command, RemoveDraft):
        if not any(d.highlight_id == command.highlight_id for d in state.drafts):
            return Rejection(reason=f"no draft for highlight: {command.highlight_id}")
        return emit(ev.DraftRemoved, highlight_id=command.highlight_id)

    if isinstance(command, MarkDraftPosted):
        if not any(d.highlight_id == command.highlight_id for d in state.drafts):
            return Rejection(reason=f"no draft for highlight: {command.highlight_id}")
        return emit(ev.DraftPosted, highlight_id=command.highlight_id, url=command.url,
                    thread_id=command.thread_id)

    if isinstance(command, ClearChat):
        return emit(ev.ChatCleared)

    if isinstance(command, EndSession):
        return emit(ev.SessionEnded)

    return Rejection(reason=f"unhandled command: {command.type}")  # pragma: no cover
