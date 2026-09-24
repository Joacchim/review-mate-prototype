"""AgentBridge — the agent's view of a session, transport-free and testable in-process.

All writes go through the actor with `Origin.AGENT`, so the core authority matrix governs what the
agent may do; the bridge simply offers no browser-only operation. `wait_for_highlight` is the watch
primitive the MCP layer exposes so the agent can react to new highlights without busy-polling.
"""
from __future__ import annotations

import asyncio
from review_mate.seams import LocalRef

from review_mate.session.actor import CommandResult
from review_mate.session.commands import (
    AddHighlight, EmitCard, LabelCard, PostMessage, RecordAddressed, RequestAccess, UpdateCard,
)
from review_mate.session.events import (
    AccessDecided, AccessGrantChanged, HighlightAdded, MessagePosted,
)
from review_mate.session.manager import SessionManager
from review_mate.session.state import (
    CardStatus, Criticality, FileEntry, Label, LineRange, Origin, Side, SessionState,
    SessionSummary, Subject, SubjectKind, Theme,
)


def _default_base_url() -> str:
    """Where a reviewer reads this server. Overridable, because the port is not always the default
    and the link is useless if it points somewhere nobody is listening."""
    import os
    return os.environ.get("REVIEW_MATE_URL", "http://127.0.0.1:8765")


def _label(theme: str | None, criticality: str | None, about: str = "") -> Label | None:
    """A label needs both halves or it is not one: a theme with no weight cannot be sorted, and a
    weight with no theme cannot be filtered. Neither given means the card is simply unclassified."""
    if theme is None and criticality is None:
        return None
    if theme is None or criticality is None:
        raise ValueError("a label needs both a theme and a criticality")
    return Label(theme=Theme(theme), criticality=Criticality(criticality), about=about)


class AgentBridge:
    def __init__(self, manager: SessionManager, broker=None, provider=None, view=None,
                 base_url: str = ""):
        self._m = manager
        self._broker = broker      # LookupBroker (MR-discovery channel); None in the baseline
        self._provider = provider  # HostProvider, for search_mrs; None when no host configured
        self._view = view          # AgentView — the folded read; injected so it shares the clients'
                                   # scope instances, and with them their caches
        self._base_url = (base_url or _default_base_url()).rstrip("/")

    def _actor(self, session_id: str):
        actor = self._m.get(session_id)
        if actor is None:
            raise KeyError(session_id)
        return actor

    # --- reads --------------------------------------------------------------

    def list_sessions(self) -> list[SessionSummary]:
        return self._m.list()

    async def open_local_review(self, path: str, branch: str, base: str = "") -> dict:
        """Open a review of a branch in a repository on this machine, and say where to read it.

        The link is the point as much as the session is: this exists so an agent can hand the
        reviewer somewhere to look at what it just wrote, and a session id alone is not that.
        """
        sid = await self._m.create(ref=LocalRef(path=path, branch=branch, base=base))
        snapshot = self._m.get(sid).snapshot()
        return {"session_id": sid, "url": f"{self._base_url}/?session={sid}",
                "branch": snapshot.mr.source_branch if snapshot.mr else branch,
                "base": snapshot.mr.target_branch if snapshot.mr else base,
                "files": len(snapshot.files)}

    def snapshot(self, session_id: str) -> SessionState:
        """The raw session. Internal — `view` is what the agent reads."""
        return self._actor(session_id).snapshot()

    async def view(self, session_id: str) -> dict:
        """The session as the agent sees it: the same folded scopes the reviewer's clients read."""
        if self._view is None:
            raise RuntimeError("this server was built without an agent view")
        return await self._view.build(session_id)

    async def diff(self, session_id: str, path: str | None = None) -> dict:
        if self._view is None:
            raise RuntimeError("this server was built without an agent view")
        return await self._view.diff(session_id, path=path)

    # --- watch --------------------------------------------------------------

    async def wait_for_highlight(self, session_id: str, since: int = 0,
                                 timeout: float | None = None) -> dict | None:
        actor = self._actor(session_id)

        async def _wait() -> dict | None:
            async for event in actor.subscribe(since=since):
                if isinstance(event, HighlightAdded):
                    return {"seq": event.seq, "highlight": event.highlight}
            return None

        if timeout is None:
            return await _wait()
        try:
            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            return None

    # --- writes (agent authority) ------------------------------------------

    async def add_highlight(self, session_id: str, file: str, start: int, end: int,
                            side: str = "new", question: str | None = None) -> CommandResult:
        """Flag a zone the agent itself wants to surface (an agent-authored highlight)."""
        return await self._actor(session_id).submit(
            AddHighlight(file=file, side=Side(side), line_range=LineRange(start=start, end=end),
                         question=question),
            Origin.AGENT,
        )

    async def add_insight(self, session_id: str, file: str, start: int, end: int, body: str,
                          side: str = "new", citations: list[str] | None = None) -> dict:
        """Surface an agent-found insight anchored to a zone: create the highlight, then its card.

        Returns {highlight_id, card} so the agent can later update_card. The highlight is authored
        by the agent (author=AGENT), so the UI marks it as Claude's and the reviewer can dismiss it.
        """
        actor = self._actor(session_id)
        before = {h.id for h in actor.snapshot().highlights}
        added = await actor.submit(
            AddHighlight(file=file, side=Side(side), line_range=LineRange(start=start, end=end)),
            Origin.AGENT,
        )
        if not added.ok:
            return {"highlight_id": None, "card": added.model_dump()}
        new = [h for h in actor.snapshot().highlights
               if h.id not in before and h.file == file and h.author is Origin.AGENT]
        hid = new[-1].id if new else None
        card = await self.emit_card(session_id, hid, body, citations)
        return {"highlight_id": hid, "card": card.model_dump()}

    async def record_addressed(self, session_id: str, subject_kind: str, subject_id: str,
                               sha: str, summary: str = "") -> CommandResult:
        return await self._actor(session_id).submit(
            RecordAddressed(subject=Subject(kind=SubjectKind(subject_kind), id=subject_id),
                            sha=sha, summary=summary),
            Origin.AGENT)

    async def label_card(self, session_id: str, card_id: str, theme: str, criticality: str,
                         about: str = "") -> CommandResult:
        return await self._actor(session_id).submit(
            LabelCard(card_id=card_id,
                      label=Label(theme=Theme(theme), criticality=Criticality(criticality),
                                  about=about)),
            Origin.AGENT)

    async def emit_card(self, session_id: str, highlight_id: str | None, body: str,
                        citations: list[str] | None = None, theme: str | None = None,
                        criticality: str | None = None, about: str = "") -> CommandResult:
        return await self._actor(session_id).submit(
            EmitCard(highlight_id=highlight_id, body=body, citations=citations or [],
                     label=_label(theme, criticality, about)),
            Origin.AGENT,
        )

    async def update_card(self, session_id: str, card_id: str, body: str | None = None,
                          status: CardStatus | None = None) -> CommandResult:
        return await self._actor(session_id).submit(
            UpdateCard(card_id=card_id, body=body, status=status), Origin.AGENT,
        )

    async def request_access(self, session_id: str, repo: str, reason: str) -> CommandResult:
        return await self._actor(session_id).submit(
            RequestAccess(repo=repo, reason=reason), Origin.AGENT,
        )

    async def access_state(self, session_id: str) -> list[dict]:
        """Every repository asked about, what the reviewer said, and what that produced.

        The agent's half of consent. Asking and then having no way to learn the answer is how a
        request becomes a guess: an agent that cannot see a refusal reads its own silence as a
        maybe, and one that cannot see a path has nothing to do with an approval.

        Off the same scope the reviewer's clients render, so the two cannot come apart.
        """
        if self._view is None:
            raise RuntimeError("this server was built without an agent view")
        return await self._view.access(session_id)

    async def wait_for_access(self, session_id: str, since: int = 0,
                              timeout: float | None = None) -> dict | None:
        """Wait for the next thing to happen to a consent request — a decision, or what it produced.

        A refusal is an answer worth waiting for too, so this returns on any of them rather than
        only on success: an agent blocked until an approval that never comes has stopped working
        with nothing said.
        """
        actor = self._actor(session_id)

        async def _wait() -> dict | None:
            async for event in actor.subscribe(since=since):
                if isinstance(event, (AccessDecided, AccessGrantChanged)):
                    req = next((r for r in actor.snapshot().access_requests
                                if r.id == event.request_id), None)
                    if req is None:
                        continue
                    grant = req.grant
                    return {"seq": event.seq, "id": req.id, "repo": req.repo,
                            "status": getattr(req.status, "value", str(req.status)),
                            "state": grant.state if grant else None,
                            "path": grant.path if grant else None,
                            "error": grant.error if grant else ""}
            return None

        if timeout is None:
            return await _wait()
        try:
            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            return None

    # --- chat ---------------------------------------------------------------

    async def post_message(self, session_id: str, body: str,
                           anchor: Subject | None = None) -> CommandResult:
        """Answer in a conversation. `anchor` names its subject; None is the review's own."""
        return await self._actor(session_id).submit(PostMessage(body=body, anchor=anchor),
                                                    Origin.AGENT)

    # --- MR lookup (the standing discovery channel) -------------------------

    async def search_mrs(self, query: str) -> list[dict]:
        """Host search for MRs matching `query` — real, loadable candidates for a lookup answer."""
        if self._provider is None or not hasattr(self._provider, "search"):
            return []
        return await self._provider.search(query)

    async def wait_for_lookup(self, since: int = 0, timeout: float | None = None) -> dict | None:
        """Wait for the reviewer's next MR-lookup request after `since`; returns {seq, id, query}."""
        if self._broker is None:
            return None
        req = await self._broker.wait_for_request(since=since, timeout=timeout)
        if req is None:
            return None
        return {"seq": req.seq, "id": req.id, "query": req.query}

    def answer_lookup(self, lookup_id: str, answer: str, candidates: list[dict] | None = None) -> dict:
        """Answer a lookup request: prose + loadable MR candidates the browser will show."""
        if self._broker is None:
            raise KeyError(lookup_id)
        return self._broker.answer(lookup_id, answer, candidates).model_dump(mode="json")

    async def wait_for_message(self, session_id: str, since: int = 0,
                               timeout: float | None = None) -> dict | None:
        """Wait for the reviewer's next chat message (role=user) after `since`."""
        actor = self._actor(session_id)

        async def _wait() -> dict | None:
            async for event in actor.subscribe(since=since):
                if isinstance(event, MessagePosted) and event.message.role == "user":
                    return {"seq": event.seq, "message": event.message.model_dump(mode="json")}
            return None

        if timeout is None:
            return await _wait()
        try:
            return await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            return None
