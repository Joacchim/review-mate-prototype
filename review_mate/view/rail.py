"""The `rail` scope: what a reviewer has asked about, and what came back.

One scope per session, carrying every highlight with its card, plus the MR-level insights. The diff
overlay selects the open file's highlights out of it; the right-hand rail lists them all.

They live together rather than inside each file's scope because the numbering is session-wide — a
reviewer references a card as "#2", which no per-file view can assign — and because a card arriving
would otherwise republish a whole tokenized file to deliver a few hundred bytes.

The cheap context tier rides here too. It is a host read per line range, so it follows the shape the
hub's queue established: the view reports it as loading, a one-shot fetch lands, and the scope
republishes. A range at a fixed sha cannot change, so what it caches never needs invalidating.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress

from pydantic import BaseModel, Field

from review_mate.session.state import DraftStatus, SessionStatus


class RailCard(BaseModel):
    id: str
    body: str = ""
    citations: list[str] = Field(default_factory=list)
    status: str = ""
    created_at: str = ""


class RailContext(BaseModel):
    state: str = "idle"          # idle | loading | ready | unavailable | error
    blame: list[dict] = Field(default_factory=list)
    linked_issues: list[dict] = Field(default_factory=list)
    error: str = ""


class RailHighlight(BaseModel):
    id: str
    n: int                       # session-wide, and what a reviewer references in chat
    file: str
    side: str = "new"
    start: int = 0
    end: int = 0
    question: str | None = None
    status: str = "open"
    author: str = "browser"          # a highlight the agent made reads differently in the rail
    context_requested: bool = False  # escalated past the cheap tier, so an answer is expected
    context_requested_at: str = ""   # when they escalated — a client ages the "working" cue from it
    stale: bool = False          # made against an earlier head, so its lines may have moved
    comment_state: str = "context"   # context | comment | posted
    created_at: str = ""
    context: RailContext = Field(default_factory=RailContext)
    card: RailCard | None = None


class ReviewPass(BaseModel):
    """The MR-wide pass: whether one was asked for, and whether asking now would say anything new.

    `stale` and `available` are not the same fact. A pass asked about code the change has moved
    past stays visible and stale — a waiting cue that disappeared with nothing arriving would be
    the worst answer available — while `available` says only that the *current* code has not been
    passed over, which is what the control reflects.
    """
    requested: bool = False
    at: str = ""
    sha: str | None = None
    stale: bool = False
    available: bool = True


class RailView(BaseModel):
    session: str
    state: str = "ready"         # ready | unknown-session
    highlights: list[RailHighlight] = Field(default_factory=list)
    insights: list[RailCard] = Field(default_factory=list)
    review_pass: ReviewPass = Field(default_factory=ReviewPass)


class RailScope:
    """Builds the rail, and owns the cheap tier's cache.

    `build` never calls the host: the tier is fetched by a one-shot task per line range, keyed on
    the sha it was read at.
    """

    def __init__(self, manager, provider=None, publish=None) -> None:
        self._manager = manager
        self._provider = provider
        self._publish = publish      # publish(session_id) -> awaitable
        self._context: dict[tuple, dict] = {}     # (sha, file, start, end) -> {blame, linked_issues}
        self._failed: dict[tuple, str] = {}
        self._tasks: dict[tuple, asyncio.Task] = {}

    async def build(self, session_id: str) -> dict:
        snapshot = self._snapshot(session_id)
        if snapshot is None:
            return RailView(session=session_id, state="unknown-session").model_dump(mode="json")
        head = snapshot.mr.sha if snapshot.mr else ""
        by_highlight = {c.highlight_id: c for c in snapshot.cards if c.highlight_id}
        drafts = {d.highlight_id: d for d in snapshot.drafts if d.highlight_id}
        rows = []
        for highlight in snapshot.highlights:
            draft = drafts.get(highlight.id)
            rows.append(RailHighlight(
                id=highlight.id, n=highlight.ordinal, file=highlight.file,
                side=getattr(highlight.side, "value", "new"),
                start=highlight.line_range.start, end=highlight.line_range.end,
                question=highlight.question,
                status=getattr(highlight.status, "value", "open"),
                author=getattr(highlight.author, "value", "browser"),
                context_requested=bool(highlight.context_requested),
                context_requested_at=highlight.context_requested_at,
                stale=bool(highlight.created_sha and head and highlight.created_sha != head),
                comment_state=("context" if draft is None else
                               "posted" if draft.status is DraftStatus.POSTED else "comment"),
                created_at=highlight.created_at,
                context=self._context_for(snapshot, highlight, session_id),
                card=self._card(by_highlight.get(highlight.id)),
            ))
        insights = [self._card(c) for c in snapshot.cards if not c.highlight_id]
        return RailView(session=session_id, highlights=rows,
                        insights=[c for c in insights if c],
                        review_pass=self._pass(snapshot, head)).model_dump(mode="json")

    @staticmethod
    def _pass(snapshot, head: str) -> ReviewPass:
        if not snapshot.insights_requested:
            return ReviewPass(available=True)
        sha = snapshot.insights_requested_sha
        stale = bool(sha and head and sha != head)
        # the current code has not been passed over if the pass was about something else
        return ReviewPass(requested=True, at=snapshot.insights_requested_at, sha=sha,
                          stale=stale, available=stale)

    @staticmethod
    def _card(card) -> RailCard | None:
        if card is None:
            return None
        return RailCard(id=card.id, body=card.body, citations=list(card.citations),
                        status=getattr(card.status, "value", ""), created_at=card.created_at)

    # --- the cheap tier ------------------------------------------------------

    def _context_for(self, snapshot, highlight, session_id: str) -> RailContext:
        if snapshot.mr is None:
            return RailContext(state="unavailable")
        key = (snapshot.mr.sha, highlight.file, highlight.line_range.start, highlight.line_range.end)
        if key in self._failed:
            return RailContext(state="error", error=self._failed[key])
        found = self._context.get(key)
        if found is not None:
            return RailContext(state="ready", blame=found["blame"],
                               linked_issues=found["linked_issues"])
        if self._provider is None or not hasattr(self._provider, "blame"):
            return RailContext(state="unavailable")
        self._start(key, snapshot.mr.project, snapshot.mr.iid, session_id)
        return RailContext(state="loading")

    def _start(self, key, project: str, iid, session_id: str) -> None:
        if key in self._tasks:
            return
        task = asyncio.create_task(self._fetch(key, project, iid, session_id))
        self._tasks[key] = task
        task.add_done_callback(lambda finished: self._finished(key, finished))

    def _finished(self, key, task) -> None:
        self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()      # retrieve it; the failure is already in the view

    async def _fetch(self, key, project: str, iid, session_id: str) -> None:
        sha, file, start, end = key
        out = {"blame": [], "linked_issues": []}
        try:
            # each source degrades on its own: a missing blame must not cost the linked issues
            with suppress(Exception):
                out["blame"] = await self._provider.blame(project, file, sha, start, end)
            if hasattr(self._provider, "linked_issues"):
                with suppress(Exception):
                    out["linked_issues"] = await self._provider.linked_issues(project, iid)
            self._context[key] = out
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed[key] = f"{type(exc).__name__}: {exc}"
        if self._publish is not None:
            await self._publish(session_id)

    # --- lifecycle -----------------------------------------------------------

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def reset(self) -> None:
        """Drop every cached line-range read."""
        self._context.clear()
        self._failed.clear()

    def _snapshot(self, session_id: str):
        actor = self._manager.get(session_id)
        if actor is None:
            return None
        snapshot = actor.snapshot()
        return snapshot if snapshot.status is SessionStatus.ACTIVE else None
