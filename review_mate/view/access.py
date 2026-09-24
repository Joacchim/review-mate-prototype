"""The `access` scope: repositories Claude has asked to read, and what was said about them.

Cross-repo context is consent-gated and agent-initiated (D-crossrepo): the agent asks, the reviewer
decides, and nothing is read until they do. So the interesting state is not "which repos are
readable" but "what is outstanding" — a question the reviewer answers and then forgets, which is
exactly the kind a client should be told rather than work out.

Decided requests ride along rather than being dropped. A reviewer who denied something wants to see
that they denied it, and an agent re-asking for what was refused reads very differently from an
agent asking for the first time.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.session.state import SessionStatus


class AccessRow(BaseModel):
    id: str
    repo: str
    reason: str = ""
    status: str = "pending"        # pending | approved | denied
    decided_at: str | None = None


class AccessView(BaseModel):
    session: str
    state: str = "ready"           # ready | unknown-session
    requests: list[AccessRow] = Field(default_factory=list)
    pending: int = 0


class AccessScope:
    """Builds the consent list. Reads the session and nothing else — asking is the agent's move and
    deciding is the reviewer's, so there is no host here at all."""

    def __init__(self, manager) -> None:
        self._manager = manager

    async def build(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return AccessView(session=session_id, state="unknown-session").model_dump(mode="json")
        rows = [AccessRow(id=r.id, repo=r.repo, reason=r.reason,
                          status=getattr(r.status, "value", str(r.status)),
                          decided_at=r.decided_at)
                for r in (snapshot.access_requests or [])]
        return AccessView(session=session_id, requests=rows,
                          pending=sum(1 for r in rows if r.status == "pending"),
                          ).model_dump(mode="json")

    def _snapshot(self, session_id: str):
        actor = self._manager.get(session_id)
        if actor is None:
            return None
        snapshot = actor.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
