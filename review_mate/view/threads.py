"""The `threads` scope: the discussions already on the merge request.

The host owns these, not the session — a discussion exists because someone wrote it on the MR, and
the session mirrors what the host last reported. So this carries what a reviewer needs to read and
answer one: where it is anchored, who said what, whether it is resolved, and which of its comments
are theirs to edit.

Whose a comment is was a separate question a client asked (`GET /api/me`) and then answered by
comparing usernames. The server knows who is reviewing, so each comment says `mine` and no client
compares anything. A client that got that wrong would offer an edit the host then refuses.

Counts ride along for the same reason: the filter a reviewer reaches for is "what is still open",
and a count derived per client is a count two clients can disagree about.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from review_mate.session.state import SessionStatus


class ThreadCommentView(BaseModel):
    id: str
    author: str
    body: str
    created_at: str = ""
    mine: bool = False           # the reviewer's own, so editing and deleting are offered


class ThreadView(BaseModel):
    id: str
    anchor: dict | None = None   # {file, side, line}, or None for a discussion about the whole MR
    resolved: bool = False
    capabilities: dict[str, bool] = Field(default_factory=dict)
    comments: list[ThreadCommentView] = Field(default_factory=list)


class ThreadsView(BaseModel):
    session: str
    state: str = "ready"         # ready | unknown-session
    threads: list[ThreadView] = Field(default_factory=list)
    unresolved: int = 0
    total: int = 0


class ThreadsScope:
    """Builds the discussion list. Never reads the host: the session holds what the last re-sync
    mirrored, and re-syncing is a command rather than something a rebuild does behind the scenes."""

    def __init__(self, manager, user: str = "") -> None:
        self._manager = manager
        self._user = user or ""

    async def build(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return ThreadsView(session=session_id, state="unknown-session").model_dump(mode="json")
        threads = [self._thread(t) for t in (snapshot.threads or [])]
        return ThreadsView(
            session=session_id, threads=threads,
            unresolved=sum(1 for t in threads if not t.resolved),
            total=len(threads),
        ).model_dump(mode="json")

    def _thread(self, thread) -> ThreadView:
        return ThreadView(
            id=thread.id, anchor=thread.anchor, resolved=bool(thread.resolved),
            capabilities=dict(thread.capabilities or {}),
            comments=[self._comment(c) for c in (thread.comments or [])],
        )

    def _comment(self, comment) -> ThreadCommentView:
        return ThreadCommentView(
            id=comment.id, author=comment.author, body=comment.body,
            created_at=comment.created_at,
            mine=bool(self._user) and comment.author == self._user,
        )

    def _snapshot(self, session_id: str):
        writer = self._manager.get(session_id)
        if writer is None:
            return None
        snapshot = writer.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
