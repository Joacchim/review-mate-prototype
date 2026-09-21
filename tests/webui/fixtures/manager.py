"""A session manager the tests can set directly.

The fixture server is the production `create_app`; only this sits underneath it. Every frame the
browser receives is therefore built by the real scope builders and validated by the real models,
so there is no second description of the protocol to drift from the first.

Commands are real too: `submit` runs the production `handle()` and `reduce()`, so a click in the
browser lands in staged state by the same path it takes in production, and the session tail
republishes the scopes that hold it. What is faked is durability — there is no event log, seq is
counted here — and the host beneath it.

Scenarios are built from the real state models, never from dicts.
"""
from __future__ import annotations

import asyncio

from review_mate.session.actor import CommandResult
from review_mate.session.commands import Rejection, handle
from review_mate.session.reducer import reduce
from review_mate.session.state import Origin, SessionState, SessionStatus, SessionSummary

# What the application reaches for on a manager. Both this fake and the real SessionManager must
# carry all of it — a rename on either side has to break a test, not a page.
MANAGER_SURFACE = ("get", "list", "create", "end", "restore_all", "shutdown",
                   "_activity_broker", "_workspace")


class FakeActor:
    """One session, applying commands for real and broadcasting what they produced."""

    def __init__(self, state: SessionState) -> None:
        self._state = state
        self._subscribers: set[asyncio.Queue] = set()
        self.commands: list[tuple] = []      # (command, origin), for a test that asserts the ask

    def snapshot(self) -> SessionState:
        return self._state

    async def submit(self, command, origin: Origin = Origin.BROWSER) -> CommandResult:
        self.commands.append((command, origin))
        outcome = handle(self._state, command, origin)
        if isinstance(outcome, Rejection):
            return CommandResult(ok=False, reason=outcome.reason)
        for event in outcome:
            event.seq = self._state.seq + 1
            self._state = reduce(self._state, event)
            for queue in list(self._subscribers):
                queue.put_nowait(event)
        return CommandResult(ok=True, seq=self._state.seq)

    async def subscribe(self, since: int = 0):
        """The per-session event stream: live events only.

        A staged session has no log to replay from, and `since` is the seq the caller already
        holds — the application subscribes at the current snapshot, so there is nothing behind it.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


class FakeManager:
    """Holds sessions as actors over state objects. `create` mints an empty one unless staged."""

    def __init__(self) -> None:
        self._actors: dict[str, FakeActor] = {}
        self._activity_broker = None
        self._workspace = None
        self.created: list = []
        self.ended: list[str] = []
        self.next_id = "staged-session"

    # --- staging ----------------------------------------------------------

    def put(self, state: SessionState) -> str:
        """Stage a session. One actor per id, kept across `get` calls so a subscriber registered
        by the session tail still hears the command the browser sends."""
        self._actors[state.id] = FakeActor(state)
        return state.id

    def actor(self, session_id: str) -> FakeActor | None:
        """The staged actor itself — for a test that reads the commands a page sent."""
        return self._actors.get(session_id)

    def reset(self) -> None:
        self._actors.clear()
        self.created.clear()
        self.ended.clear()

    # --- the surface the application uses ----------------------------------

    def get(self, session_id: str):
        return self._actors.get(session_id)

    def list(self) -> list[SessionSummary]:
        out = []
        for actor in self._actors.values():
            state = actor.snapshot()
            out.append(SessionSummary(
                id=state.id, status=state.status, created_at=state.created_at, seq=state.seq,
                title=state.mr.title if state.mr else None,
                project=state.mr.project if state.mr else None,
                iid=state.mr.iid if state.mr else None,
                url=state.mr.url if state.mr else None,
                highlights=len(state.highlights), cards=len(state.cards),
                drafts_pending=sum(1 for d in state.drafts if d.status.value == "draft"),
                drafts_posted=sum(1 for d in state.drafts if d.status.value == "posted"),
            ))
        return out

    async def create(self, ref=None) -> str:
        self.created.append(ref)
        state = SessionState(id=self.next_id, status=SessionStatus.ACTIVE,
                             created_at="2026-01-01T00:00:00+00:00", seq=0)
        self._actors[state.id] = FakeActor(state)
        return state.id

    async def end(self, session_id: str) -> None:
        if session_id not in self._actors:
            raise KeyError(session_id)
        self.ended.append(session_id)
        self._actors.pop(session_id)

    async def restore_all(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None
