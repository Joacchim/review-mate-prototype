"""CrossRepoBroker — turns an approved access request into a checkout on disk (D13).

The consent invariant is the point: a repository is materialized only after the reviewer approved
the agent's request to read it. The broker watches for that approval and does the work, recording
what it produced on the session so every client and the agent read the same answer.

It keeps nothing of its own. What was granted, where it landed and why it failed live on the
session's access request, which is the only copy — a broker-side dict would be a second source of
truth for a fact the reviewer is looking at, and the two would disagree the first time a session was
restored from its log.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from review_mate.kb.store import ReviewKB
from review_mate.seams import CheckoutHandle, RepoRef, Workspace
from review_mate.session.commands import RecordGrant
from review_mate.session.events import AccessDecided
from review_mate.session.manager import SessionManager
from review_mate.session.state import AccessStatus, Grant, Origin

logger = logging.getLogger(__name__)

# a repository name -> where to clone it and at which ref, or None when nothing answers to it
Resolver = Callable[[str], Awaitable[tuple[RepoRef, str] | None]]


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class CrossRepoBroker:
    def __init__(self, manager: SessionManager, workspace: Workspace, kb: ReviewKB,
                 resolve: Resolver):
        self._m = manager
        self._workspace = workspace
        self.kb = kb
        self._resolve = resolve
        self._grants: set[asyncio.Task] = set()   # held: a bare create_task can be collected mid-clone

    async def grant_access(self, session_id: str, request_id: str) -> CheckoutHandle | None:
        """Materialize an approved request, reporting each step onto the session.

        Reports `materializing` before doing anything slow, so "the reviewer said yes and a clone is
        running" is distinguishable from "the reviewer said yes and nothing is listening" — a clone
        of a large repository takes long enough that the difference is all the reviewer has to go on.
        """
        actor = self._m.get(session_id)
        if actor is None:
            return None
        req = next((r for r in actor.snapshot().access_requests if r.id == request_id), None)
        if req is None or req.status is not AccessStatus.APPROVED:
            return None  # the command layer refuses this too; declining here saves the round trip

        await self._record(actor, request_id, Grant(state="materializing", at=_now()))
        try:
            located = await self._resolve(req.repo)
            if located is None:
                raise LookupError(f"no repository answers to {req.repo!r}")
            ref, commit = located
            result = self._workspace.materialize(ref, commit)
            handle = await result if isinstance(result, Awaitable) else result
        except Exception as exc:
            await self._record(actor, request_id,
                               Grant(state="failed", error=f"{type(exc).__name__}: {exc}",
                                     at=_now()))
            raise

        await self._record(actor, request_id,
                           Grant(state="ready", path=handle.path, at=_now()))
        mr = actor.snapshot().mr
        if mr is not None and mr.project:
            self.kb.record_relationship(mr.project, req.repo, note=req.reason)
        return handle

    def granted(self, session_id: str, repo: str) -> str | None:
        """Where `repo` was checked out for this session, or None if it was not.

        Read off the session rather than remembered here, so a restored session answers the same as
        a live one and there is one place to be wrong.
        """
        actor = self._m.get(session_id)
        if actor is None:
            return None
        for req in actor.snapshot().access_requests or []:
            if req.repo == repo and req.status is AccessStatus.APPROVED:
                if req.grant is not None and req.grant.state == "ready":
                    return req.grant.path
        return None

    async def catch_up(self, session_id: str) -> None:
        """Materialize any approval that never produced anything.

        A grant is the one step of the consent path that outlives the process doing it: the reviewer
        approves, a clone starts, and a restart in between leaves an approval nothing is acting on.
        The decision is durable, so the work is re-derivable from it — the same argument that makes
        the ephemeral activity stream safe.
        """
        actor = self._m.get(session_id)
        if actor is None:
            return
        for req in actor.snapshot().access_requests or []:
            if req.status is AccessStatus.APPROVED and (
                    req.grant is None or req.grant.state == "materializing"):
                self._spawn(session_id, req.id)

    async def watch(self, session_id: str) -> None:
        actor = self._m.get(session_id)
        if actor is None:
            return
        await self.catch_up(session_id)
        async for event in actor.subscribe(since=actor.snapshot().seq):
            if isinstance(event, AccessDecided) and event.status is AccessStatus.APPROVED:
                self._spawn(session_id, event.request_id)

    def _spawn(self, session_id: str, request_id: str) -> None:
        """Off the consumer: a clone takes seconds, and awaiting it in the watch loop would stall
        every later event on this session behind it — and a failure would kill the loop."""
        task = asyncio.create_task(self._safe_grant(session_id, request_id))
        self._grants.add(task)
        task.add_done_callback(self._grants.discard)

    async def _record(self, actor, request_id: str, grant: Grant) -> None:
        await actor.submit(RecordGrant(request_id=request_id, grant=grant), Origin.SYSTEM)

    async def _safe_grant(self, session_id: str, request_id: str) -> None:
        try:
            await self.grant_access(session_id, request_id)
        except Exception:
            logger.warning("cross-repo grant failed for %s/%s", session_id, request_id,
                           exc_info=True)
