"""The client-facing view protocol.

A client opens one stream, names the scopes it is showing, and receives whole-scope
replacements. The scope — not the field — is the unit of change, so a client needs no patch
algebra and holds no domain logic: it renders what a scope carries.

`seq` is per-scope and monotonic. It orders replacements and exposes a gap; it is not a resume
token, because there is nothing to resume — a reconnecting client re-subscribes and is sent the
current view of every scope it names.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

HUB = "hub"


class ScopeUpdate(BaseModel):
    """A whole-scope replacement — the only state-bearing message the server sends."""
    type: Literal["scope"] = "scope"
    scope: str
    seq: int
    view: dict[str, Any]

    @property
    def key(self) -> str:
        return f"scope:{self.scope}"


class ScopeError(BaseModel):
    """A scope could not be built, or was not recognised. Never fatal to the connection."""
    type: Literal["error"] = "error"
    scope: str | None = None
    reason: str

    @property
    def key(self) -> str:
        return f"error:{self.scope or ''}"


Message = ScopeUpdate | ScopeError


class Subscribe(BaseModel):
    action: Literal["subscribe"]
    scopes: list[str] = Field(default_factory=list)


class Unsubscribe(BaseModel):
    action: Literal["unsubscribe"]
    scopes: list[str] = Field(default_factory=list)


ClientMessage = Subscribe | Unsubscribe


def parse_client_message(raw: Any) -> ClientMessage:
    """Parse one inbound client frame, or raise ValueError.

    Unknown actions are a client bug, not a transport failure — the caller answers with a
    ScopeError and keeps the stream open.
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
