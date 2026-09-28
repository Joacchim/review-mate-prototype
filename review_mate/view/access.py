"""The `access` topic: repositories Claude has asked to read, and what was said about them.

Cross-repo context is consent-gated and agent-initiated (D-crossrepo): the agent asks, the reviewer
decides, and nothing is read until they do. So the interesting state is not "which repos are
readable" but "what is outstanding" — a question the reviewer answers and then forgets, which is
exactly the kind a client should be told rather than work out.

Decided requests ride along rather than being dropped. A reviewer who denied something wants to see
that they denied it, and an agent re-asking for what was refused reads very differently from an
agent asking for the first time.

An approval carries what it produced, because saying yes is not the end of it: the repository has to
be cloned, which takes seconds and can fail. A reviewer who approved and then sees nothing has no
way to tell a clone in flight from a clone that never started.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.session.state import SessionStatus


class GrantRow(BaseModel):
    """What an approval produced. Absent while nothing has started producing anything."""
    state: str = "materializing"   # materializing | ready | failed
    path: str | None = None
    error: str = ""


class AccessRow(BaseModel):
    id: str
    repo: str
    reason: str = ""
    status: str = "pending"        # pending | approved | denied
    decided_at: str | None = None
    grant: GrantRow | None = None


class AccessView(BaseModel):
    session: str
    state: str = "ready"           # ready | unknown-session
    requests: list[AccessRow] = Field(default_factory=list)
    pending: int = 0
    working: int = 0               # approvals still being materialized


def _grant(grant) -> GrantRow | None:
    """A client renders a grant it is given and never infers one.

    In particular it cannot infer "still working" from an approval with no grant: that is the shape
    of an approval nothing is acting on, and telling the two apart is the reviewer's only signal
    that a clone is under way.
    """
    if grant is None:
        return None
    return GrantRow(state=grant.state, path=grant.path, error=grant.error)


class AccessTopic:
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
                          decided_at=r.decided_at,
                          grant=_grant(r.grant))
                for r in (snapshot.access_requests or [])]
        return AccessView(
            session=session_id, requests=rows,
            pending=sum(1 for r in rows if r.status == "pending"),
            working=sum(1 for r in rows if r.grant is not None
                        and r.grant.state == "materializing"),
        ).model_dump(mode="json")

    def _snapshot(self, session_id: str):
        writer = self._manager.get(session_id)
        if writer is None:
            return None
        snapshot = writer.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
