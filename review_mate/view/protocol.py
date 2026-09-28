"""The client-facing view protocol.

A client opens one stream, names the topics it is showing, and receives whole-topic
replacements. The topic — not the field — is the unit of change, so a client needs no patch
algebra and holds no domain logic: it renders what a topic carries.

`seq` is per-topic and monotonic. It orders replacements and exposes a gap; it is not a resume
token, because there is nothing to resume — a reconnecting client re-subscribes and is sent the
current view of every topic it names. Not the event log's `seq`, which is an offset a client
genuinely does resume from: the two share a name and answer that question oppositely.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

HUB = "hub"


class TopicUpdate(BaseModel):
    """A whole-topic replacement — the only state-bearing message the server sends."""
    type: Literal["topic"] = "topic"
    topic: str
    seq: int
    view: dict[str, Any]

    @property
    def key(self) -> str:
        return f"topic:{self.topic}"


class TopicError(BaseModel):
    """A topic could not be built, or was not recognised. Never fatal to the connection."""
    type: Literal["error"] = "error"
    topic: str | None = None
    reason: str

    @property
    def key(self) -> str:
        return f"error:{self.topic or ''}"


Message = TopicUpdate | TopicError


class Subscribe(BaseModel):
    action: Literal["subscribe"]
    topics: list[str] = Field(default_factory=list)


class Unsubscribe(BaseModel):
    action: Literal["unsubscribe"]
    topics: list[str] = Field(default_factory=list)


ClientMessage = Subscribe | Unsubscribe


def parse_client_message(raw: Any) -> ClientMessage:
    """Parse one inbound client frame, or raise ValueError.

    Unknown actions are a client bug, not a transport failure — the caller answers with a
    TopicError and keeps the stream open.
    """
    if not isinstance(raw, dict):
        raise ValueError("expected an object")
    action = raw.get("action")
    model = {"subscribe": Subscribe, "unsubscribe": Unsubscribe}.get(action)
    if model is None:
        raise ValueError(f"unknown action: {action!r}")
    try:
        return model(**raw)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
