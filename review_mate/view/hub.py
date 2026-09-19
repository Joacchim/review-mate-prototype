"""The `hub` scope: what a client shows before a review is open.

Composes what the browser used to assemble from five endpoints — the open reviews, the
per-review verdict, and the host review queue — into one document a client renders as-is.

The three pieces do not share a freshness model, and collapsing them would change behaviour:

- **sessions** are local, and rebuilt on every publish.
- **the queue** is a host read, fetched once when a client first watches the scope, and
  republished when it lands so a slow host never delays the local half.
- **per-review host facts** (MR state, head, unresolved count) are a fan-out across every open
  review, and refresh only on an explicit `hub.refresh` — a manual check for updates, never a
  background poll (D19). Until one runs, a review reports `host_checked: false`.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from review_mate.seams import MRRef
from review_mate.session.state import DraftStatus, SessionStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derive_state(*, mr_state: str, pending: int, posted: int, unresolved: int,
                 behind: bool, at_watermark: bool) -> str:
    """The landing-hub verdict for one open review, most-decisive first."""
    if mr_state == "merged":
        return "merged"            # the MR landed — done (reviewer removes it)
    if mr_state == "closed":
        return "closed"            # closed without merging
    if pending:
        return "in_progress"       # you have unsubmitted comments
    if behind:
        return "git_update"        # branch advanced past your review
    if unresolved:
        return "discussions"       # open discussions, no git change
    if at_watermark or posted:
        return "reviewed"          # up to date
    return "new"                   # nothing established yet


class HubMR(BaseModel):
    host: str
    project: str
    iid: int
    title: str = ""
    url: str = ""
    author: str = ""


class HubSession(BaseModel):
    id: str
    status: str
    created_at: str = ""
    mr: HubMR | None = None
    state: str = "new"
    mr_state: str = ""
    behind: bool = False
    unresolved: int = 0
    pending: int = 0
    posted: int = 0
    highlights: int = 0
    cards: int = 0
    host_checked: bool = False


class HubView(BaseModel):
    user: str = ""
    sessions: list[HubSession] = Field(default_factory=list)
    queue: list[dict] = Field(default_factory=list)
    queue_state: str = "idle"          # idle | loading | ready | error
    queue_error: str = ""
    host_checked_at: str = ""          # last hub.refresh, empty until one runs


class HubScope:
    """Builds the hub view and owns the cache of host-derived facts.

    `build` never calls the host: it folds live local state over whatever the last refresh left
    behind. Every host read is an explicit method, so a scope rebuild can never turn into a
    fan-out of network calls.
    """

    def __init__(self, manager, provider=None, kb=None, user: str = "") -> None:
        self._manager = manager
        self._provider = provider
        self._kb = kb
        self._user = user
        self._host: dict[str, dict] = {}     # session id -> {head, mr_state, unresolved}
        self._queue: list[dict] = []
        self._queue_state = "idle"
        self._queue_error = ""
        self._checked_at = ""
        self._queue_task: asyncio.Task | None = None

    async def build(self) -> dict:
        sessions = []
        for summ in self._manager.list():
            if summ.status is not SessionStatus.ACTIVE:
                continue
            actor = self._manager.get(summ.id)
            if actor is None:
                continue
            sessions.append(self._fold(summ.id, actor.snapshot()))
        # newest first — row order is a view decision, so both clients get the same one
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return HubView(user=self._user, sessions=sessions, queue=list(self._queue),
                       queue_state=self._queue_state, queue_error=self._queue_error,
                       host_checked_at=self._checked_at).model_dump(mode="json")

    def _fold(self, sid: str, snap) -> HubSession:
        pending = sum(1 for d in snap.drafts if d.status is DraftStatus.DRAFT)
        posted = sum(1 for d in snap.drafts if d.status is DraftStatus.POSTED)
        counts = dict(highlights=len(snap.highlights), cards=len(snap.cards),
                      pending=pending, posted=posted)
        if snap.mr is None:
            return HubSession(id=sid, status="active", created_at=snap.created_at, **counts,
                              state=derive_state(mr_state="", pending=pending, posted=posted,
                                                 unresolved=0, behind=False, at_watermark=False))
        host = self._host.get(sid, {})
        head = host.get("head") or snap.mr.sha
        mr_state = host.get("mr_state", "")
        unresolved = host.get("unresolved", 0)
        wm = (self._kb.get_watermark(snap.mr.host, snap.mr.project, snap.mr.iid)
              if self._kb is not None else None)
        behind = bool(wm and head and wm != head)
        at_watermark = bool(wm and head and wm == head)
        return HubSession(
            id=sid, status="active", created_at=snap.created_at,
            mr=HubMR(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid,
                     title=snap.mr.title, url=snap.mr.url, author=snap.mr.author),
            state=derive_state(mr_state=mr_state, pending=pending, posted=posted,
                               unresolved=unresolved, behind=behind, at_watermark=at_watermark),
            mr_state=mr_state, behind=behind, unresolved=unresolved,
            host_checked=sid in self._host, **counts,
        )

    def ensure_queue(self, publish) -> asyncio.Task | None:
        """Start the one-shot queue read the first time a client watches the scope.

        Returns the in-flight task so a caller that needs the result — a test, or a command
        that must not answer before the queue is current — can await it.
        """
        if self._queue_task is not None:
            return self._queue_task
        if self._queue_state == "ready":
            return None
        self._queue_state = "loading"
        self._queue_task = asyncio.create_task(self._load_queue(publish))
        self._queue_task.add_done_callback(self._queue_done)
        return self._queue_task

    def _queue_done(self, task: asyncio.Task) -> None:
        self._queue_task = None
        if not task.cancelled():
            task.exception()   # retrieve it, so a failure never surfaces as an unhandled task

    async def aclose(self) -> None:
        """Cancel an in-flight queue read. The scope outlives no server: a read still running at
        shutdown is abandoned deliberately here rather than cancelled out from under itself."""
        task = self._queue_task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _load_queue(self, publish) -> None:
        if self._provider is None or not hasattr(self._provider, "review_queue_items"):
            self._queue, self._queue_state = [], "ready"   # no host → empty queue, not an error
        else:
            try:
                items = await self._provider.review_queue_items()
                self._queue = list(items or [])
                self._queue_state = "ready"
            except asyncio.CancelledError:
                self._queue_state = "idle"   # shutdown, not a host failure — leave it re-readable
                raise
            except Exception as exc:
                self._queue, self._queue_state = [], "error"
                self._queue_error = f"{type(exc).__name__}: {exc}"
        await publish()

    def invalidate_queue(self) -> None:
        """Drop the cached queue so the next `ensure_queue` re-reads it from the host."""
        self._queue_state = "idle"

    async def refresh(self) -> None:
        """The manual host fan-out across open reviews (D19). One `mr_summary` + one thread
        read per active review, each best-effort: a review whose host read fails keeps the
        facts it had rather than reverting to unchecked."""
        if self._provider is None:
            self._checked_at = _now()
            return
        for summ in self._manager.list():
            if summ.status is not SessionStatus.ACTIVE:
                continue
            actor = self._manager.get(summ.id)
            if actor is None:
                continue
            snap = actor.snapshot()
            if snap.mr is None:
                continue
            ref = MRRef(host=snap.mr.host, project=snap.mr.project, iid=snap.mr.iid)
            facts = dict(self._host.get(summ.id, {}))
            facts.setdefault("head", snap.mr.sha)
            facts.setdefault("mr_state", "")
            facts.setdefault("unresolved", 0)
            if hasattr(self._provider, "mr_summary"):
                try:
                    summary = await self._provider.mr_summary(ref)
                    facts["head"] = summary.get("head") or facts["head"]
                    facts["mr_state"] = summary.get("state", facts["mr_state"])
                except Exception:
                    pass
            elif hasattr(self._provider, "mr_versions"):
                try:
                    versions = await self._provider.mr_versions(ref)
                    if versions and versions[0].get("head_sha"):
                        facts["head"] = versions[0]["head_sha"]
                except Exception:
                    pass
            if hasattr(self._provider, "fetch_threads"):
                try:
                    facts["unresolved"] = sum(
                        1 for t in await self._provider.fetch_threads(ref) if not t.resolved)
                except Exception:
                    pass
            self._host[summ.id] = facts
        self._checked_at = _now()

    def forget(self, session_id: str) -> None:
        self._host.pop(session_id, None)

    def reset(self) -> None:
        """Drop every cached host fact, so the next build reports nothing as checked."""
        self._host.clear()
        self._queue = []
        self._queue_state = "idle"
        self._queue_error = ""
        self._checked_at = ""
