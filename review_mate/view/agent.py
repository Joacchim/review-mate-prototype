"""One folded view of a session for the agent — the same scopes the reviewer's clients read.

The agent used to read the session's raw state: every event-sourced field, the whole diff again, and
whatever else happened to be on the model. That made it the only party seeing an unfolded session,
and the cost showed up twice. Facts got derived a second time — the worker recomputed by hand the
backlog `view.asks` already publishes — and things the reviewer never meant to share were simply
there, because nothing had ever decided what the agent's view contained.

So it is composed here, deliberately, out of the builders the clients already use:

- **the rail** — what the reviewer marked, what was escalated, what has a card, and the MR-wide pass
- **the chat index** — the conversations, and `asks`: the backlog, published rather than re-derived
- **the discussions** — what has been said on the merge request, for everyone
- **the consent list** — which repositories were asked for and what came of each

Two things are deliberately absent. The **diff** has its own tool: it is large, it changes on a
different clock, and sending it with every read of the session was most of the payload. And the
**drafts** — the reviewer's prepared comments — are not here at any stage. A draft is private prose
written expecting no reader; once posted it is a discussion, and the agent reads it there like
everyone else. That is the whole rule, and it needs no filter: the review scope simply is not part
of this view.
"""
from __future__ import annotations

from review_mate.session.state import SessionStatus


class AgentView:
    """Builds the agent's read of a session. Owns no state — the scopes it composes own theirs."""

    def __init__(self, manager, rail=None, chat=None, threads=None, access=None) -> None:
        self._manager = manager
        self._rail = rail
        self._chat = chat
        self._threads = threads
        self._access = access

    async def build(self, session_id: str) -> dict:
        actor = self._manager.get(session_id)
        if actor is None:
            return {"session": session_id, "state": "unknown-session"}
        snapshot = actor.snapshot()
        view = {
            "session": session_id,
            "state": "ready" if snapshot.status is SessionStatus.ACTIVE else "ended",
            "mr": snapshot.mr.model_dump(mode="json") if snapshot.mr else None,
            # the on-disk worktree the agent reads code from — a session fact, not a scope's
            "checkout_path": snapshot.checkout_path,
        }
        for name, scope, argument in (("rail", self._rail, session_id),
                                      ("chat", self._chat, session_id),
                                      ("threads", self._threads, session_id),
                                      ("access", self._access, session_id)):
            if scope is not None:
                view[name] = await scope.build(argument)
        return view

    async def access(self, session_id: str) -> list[dict]:
        """Just the consent list, for an agent that only wants to know where it may read.

        Off the same scope the reviewer's clients render, so what the agent believes it was granted
        and what they see themselves granting cannot come apart.
        """
        if self._access is None:
            return []
        built = await self._access.build(session_id)
        return [
            {"id": row["id"], "repo": row["repo"], "reason": row["reason"],
             "status": row["status"],
             "state": (row.get("grant") or {}).get("state"),
             "path": (row.get("grant") or {}).get("path"),
             "error": (row.get("grant") or {}).get("error", "")}
            for row in built.get("requests", [])
        ]
