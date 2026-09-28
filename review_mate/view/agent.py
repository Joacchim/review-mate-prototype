"""One folded view of a session for the agent — the same scopes the reviewer's clients read.

The agent used to read the session's raw state: every event-sourced field, the whole diff again, and
whatever else happened to be on the model. That made it the only party seeing an unfolded session,
and the cost showed up twice. Facts got derived a second time — the worker recomputed by hand the
backlog `view.asks` already publishes — and things the reviewer never meant to share were simply
there, because nothing had ever decided what the agent's view contained.

So it is composed here, deliberately, out of the builders the clients already use:

- **the annotations** — what the reviewer marked, what was escalated, what has a card, and the MR-wide pass
- **the chat index** — the chats, and `asks`: the backlog, published rather than re-derived
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

    def __init__(self, manager, annotations=None, chat=None, threads=None, access=None,
                 diffs=None) -> None:
        self._manager = manager
        self._annotations = annotations
        self._chat = chat
        self._threads = threads
        self._access = access
        self._diffs = diffs

    async def diff(self, session_id: str, path: str | None = None) -> dict:
        """The change map, or one file's text.

        The map is the point. A checkout of the merge request is on disk at `checkout_path` — a
        real worktree off the mirror — so the agent can read any file at head, recover any old side
        with `git show <base>:<path>`, and diff any pair itself; `mr.diff_refs` carries the shas to
        do it with. What none of that gives cheaply is *where to look*, which needs the base sha and
        a shell out before the agent knows anything. So that is what this answers by default.

        Asking for a `path` returns that file's unified diff. It exists for the case where there is
        no checkout — materialization is best-effort and a clone or auth failure leaves it unset,
        with the review still working over the host API. Reaching for it while a checkout exists
        buys a worse copy of what is already on disk: no surrounding lines, nothing greppable, and
        it costs context whether or not it is read.
        """
        if self._diffs is None:
            return {"state": "unavailable"}
        if path is not None:
            return await self._diffs.raw_file(session_id, path)
        return await self._diffs.build(f"{session_id}:full")

    async def build(self, session_id: str) -> dict:
        writer = self._manager.get(session_id)
        if writer is None:
            return {"session": session_id, "state": "unknown-session"}
        snapshot = writer.snapshot()
        view = {
            "session": session_id,
            "state": "ready" if snapshot.status is SessionStatus.ACTIVE else "ended",
            "mr": snapshot.mr.model_dump(mode="json") if snapshot.mr else None,
            # the on-disk worktree the agent reads code from — a session fact, not a scope's
            "checkout_path": snapshot.checkout_path,
        }
        for name, scope, argument in (("annotations", self._annotations, session_id),
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
