"""Events — the append-only facts that make up a session's history.

State is `fold(events)`. Each event is a discriminated model (tagged by `type`) carrying an
envelope (seq, ts, origin) plus its payload. `seq` is assigned by the EventLog at append time and
is the offset clients subscribe from. Persisted one-per-line as JSON (JSONL).
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from review_mate.session.state import (
    AccessRequest, AccessStatus, Card, CardStatus, ChatMessage, DraftComment, FileEntry, Highlight,
    MRMetadata, Origin, ReviewThread,
)


class _EventBase(BaseModel):
    seq: int = 0          # assigned on append; the subscription offset
    ts: str = ""
    origin: Origin


class SessionCreated(_EventBase):
    type: Literal["session_created"] = "session_created"


class MRMetadataApplied(_EventBase):
    type: Literal["mr_metadata_applied"] = "mr_metadata_applied"
    mr: MRMetadata


class CheckoutSet(_EventBase):
    type: Literal["checkout_set"] = "checkout_set"
    path: str


class FilesApplied(_EventBase):
    type: Literal["files_applied"] = "files_applied"
    files: list[FileEntry]


class HighlightAdded(_EventBase):
    type: Literal["highlight_added"] = "highlight_added"
    highlight: Highlight


class HighlightRemoved(_EventBase):
    type: Literal["highlight_removed"] = "highlight_removed"
    highlight_id: str


class ContextRequested(_EventBase):
    type: Literal["context_requested"] = "context_requested"
    highlight_id: str
    question: str | None = None


class CardEmitted(_EventBase):
    type: Literal["card_emitted"] = "card_emitted"
    card: Card


class CardUpdated(_EventBase):
    type: Literal["card_updated"] = "card_updated"
    card_id: str
    body: str | None = None
    status: CardStatus | None = None
    citations: list[str] | None = None


class CardRemoved(_EventBase):
    type: Literal["card_removed"] = "card_removed"
    card_id: str


class AccessRequested(_EventBase):
    type: Literal["access_requested"] = "access_requested"
    request: AccessRequest


class AccessDecided(_EventBase):
    type: Literal["access_decided"] = "access_decided"
    request_id: str
    status: AccessStatus
    decided_at: str


class ThreadApplied(_EventBase):
    type: Literal["thread_applied"] = "thread_applied"
    thread: ReviewThread


class ThreadsReplaced(_EventBase):
    type: Literal["threads_replaced"] = "threads_replaced"
    threads: list[ReviewThread]


class MessagePosted(_EventBase):
    type: Literal["message_posted"] = "message_posted"
    message: ChatMessage


class ChatCleared(_EventBase):
    type: Literal["chat_cleared"] = "chat_cleared"


class DraftSaved(_EventBase):
    type: Literal["draft_saved"] = "draft_saved"
    draft: DraftComment


class DraftRemoved(_EventBase):
    type: Literal["draft_removed"] = "draft_removed"
    highlight_id: str | None = None


class DraftPosted(_EventBase):
    type: Literal["draft_posted"] = "draft_posted"
    highlight_id: str | None = None
    url: str | None = None
    thread_id: str | None = None       # the discussion the posted draft became (draft-as-thread)


class SessionEnded(_EventBase):
    type: Literal["session_ended"] = "session_ended"


Event = Annotated[
    Union[
        SessionCreated, MRMetadataApplied, CheckoutSet, FilesApplied,
        HighlightAdded, HighlightRemoved, ContextRequested,
        CardEmitted, CardUpdated, CardRemoved,
        AccessRequested, AccessDecided,
        ThreadApplied, ThreadsReplaced, MessagePosted, ChatCleared,
        DraftSaved, DraftRemoved, DraftPosted, SessionEnded,
    ],
    Field(discriminator="type"),
]

_adapter: TypeAdapter[Event] = TypeAdapter(Event)


def parse_event(data: str | bytes | dict) -> Event:
    """Decode one persisted event (a JSON string/bytes or a dict) back to its typed model."""
    if isinstance(data, (str, bytes)):
        return _adapter.validate_json(data)
    return _adapter.validate_python(data)
