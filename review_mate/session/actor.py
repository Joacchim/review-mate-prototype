"""SessionActor — the per-session single writer.

A single asyncio task drains the command queue; for each command it
validates → authorizes → reduces → appends(fsync) → folds → broadcasts. Because exactly one task
mutates state, writes are serialized with no locks (AC-9). Subscribers receive events (replayed
from the log up to the subscribe point, then live), so a client folds them into state itself.
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel

from review_mate.session import events as ev
from review_mate.session.commands import Command, Rejection, handle
from review_mate.session.eventlog import EventLog
from review_mate.session.reducer import reduce
from review_mate.session.state import Origin, SessionState, SessionStatus


class CommandResult(BaseModel):
    ok: bool
    seq: int | None = None
    reason: str | None = None


_CLOSED = object()  # sentinel: tells a subscriber stream to stop (e.g. on overflow or end)

_SUBSCRIBER_BUFFER = 2000


class _Subscriber:
    __slots__ = ("q", "alive")

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_BUFFER)
        self.alive = True


class SessionActor:
    def __init__(self, session_id: str, log: EventLog, state: SessionState):
        self.id = session_id
        self._log = log
        self._state = state
        self._queue: asyncio.Queue = asyncio.Queue()
        self._subscribers: set[_Subscriber] = set()
        self._task: asyncio.Task | None = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._close_subscribers()
        self._log.close()

    # --- reads --------------------------------------------------------------

    def snapshot(self) -> SessionState:
        # reduce always returns fresh copies, so the held state is safe to hand out read-only
        return self._state

    async def subscribe(self, since: int = 0):
        sub = _Subscriber()
        self._subscribers.add(sub)
        start_seq = self._state.seq  # no await since add() → atomic under asyncio
        try:
            for event in self._log.replay():
                if since < event.seq <= start_seq:
                    yield event
            while True:
                item = await sub.q.get()
                if item is _CLOSED:
                    break
                if item.seq <= start_seq:  # guard against a replay/live overlap
                    continue
                yield item
        finally:
            self._subscribers.discard(sub)

    # --- writes (single-writer path) ----------------------------------------

    async def submit(self, command: Command, origin: Origin) -> CommandResult:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._queue.put((command, origin, fut))
        return await fut

    async def _run(self) -> None:
        while True:
            command, origin, fut = await self._queue.get()
            try:
                result = self._apply(command, origin)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad command kill the writer
                result = CommandResult(ok=False, reason=str(exc))
            if not fut.done():
                fut.set_result(result)

    def _apply(self, command: Command, origin: Origin) -> CommandResult:
        if self._state.status is SessionStatus.ENDED:
            return CommandResult(ok=False, reason="session ended")
        outcome = handle(self._state, command, origin)
        if isinstance(outcome, Rejection):
            return CommandResult(ok=False, reason=outcome.reason)
        last_seq = self._state.seq
        for event in outcome:
            last_seq = self._log.append(event)      # assigns seq, fsync before return
            self._state = reduce(self._state, event)
            self._broadcast(event)
            if isinstance(event, ev.SessionEnded):
                self._close_subscribers()
        return CommandResult(ok=True, seq=last_seq)

    # --- broadcast ----------------------------------------------------------

    def _broadcast(self, event: "ev.Event") -> None:
        for sub in list(self._subscribers):
            if not sub.alive:
                self._subscribers.discard(sub)
                continue
            try:
                sub.q.put_nowait(event)
            except asyncio.QueueFull:
                # slow consumer: drop it; it can re-subscribe from `since` (events replayable)
                sub.alive = False
                try:
                    sub.q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
                sub.q.put_nowait(_CLOSED)
                self._subscribers.discard(sub)

    def _close_subscribers(self) -> None:
        for sub in list(self._subscribers):
            sub.alive = False
            try:
                sub.q.put_nowait(_CLOSED)
            except asyncio.QueueFull:  # pragma: no cover
                pass
