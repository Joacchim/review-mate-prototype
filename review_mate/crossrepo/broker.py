"""CrossRepoBroker — turns an approved access request into scoped read access (D13).

Honours the consent invariant: it materializes a sibling repo only after the reviewer approved the
agent's request. On a grant it materializes via workspace-manager, records the relationship in
review-kb (so discovery compounds), and tracks the grant for the session.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from review_mate.kb.store import ReviewKB
from review_mate.seams import CheckoutHandle, RepoRef, Workspace
from review_mate.session.events import AccessDecided
from review_mate.session.manager import SessionManager
from review_mate.session.state import AccessStatus

# project name -> (where to clone it, at which commit)
Resolver = Callable[[str], tuple[RepoRef, str]]


class CrossRepoBroker:
    def __init__(self, manager: SessionManager, workspace: Workspace, kb: ReviewKB,
                 resolve: Resolver):
        self._m = manager
        self._workspace = workspace
        self.kb = kb
        self._resolve = resolve
        self._grants: dict[str, dict[str, CheckoutHandle]] = {}

    async def grant_access(self, session_id: str, request_id: str) -> CheckoutHandle | None:
        actor = self._m.get(session_id)
        if actor is None:
            return None
        snap = actor.snapshot()
        req = next((r for r in snap.access_requests if r.id == request_id), None)
        if req is None or req.status is not AccessStatus.APPROVED:
            return None  # consent invariant: only an approved request materializes

        ref, commit = self._resolve(req.repo)
        result = self._workspace.materialize(ref, commit)
        handle = await result if isinstance(result, Awaitable) else result
        self._grants.setdefault(session_id, {})[req.repo] = handle

        src = snap.mr.project if snap.mr else ""
        if src:
            self.kb.record_relationship(src, req.repo, note=req.reason)
        return handle

    def is_granted(self, session_id: str, repo: str) -> bool:
        return repo in self._grants.get(session_id, {})

    def granted_checkout(self, session_id: str, repo: str) -> CheckoutHandle | None:
        return self._grants.get(session_id, {}).get(repo)

    async def watch(self, session_id: str) -> None:
        actor = self._m.get(session_id)
        if actor is None:
            return
        async for event in actor.subscribe(since=actor.snapshot().seq):
            if isinstance(event, AccessDecided) and event.status is AccessStatus.APPROVED:
                await self.grant_access(session_id, event.request_id)
