"""ActivityBroker — the global, ephemeral notification spine the agent watches (review-fleet, D16).

One stream tells the agent *something agent-relevant happened* — a highlight or message in some
session, or a lookup opened — so a single watcher covers every session instead of one long-poll per
session. It mirrors `LookupBroker`: a single monotonic `seq` and a long-poll waiter, surfaced over
HTTP at `GET /api/activity` (the agent-facing surface; see the bridge-server routes).

Notification is a distinct concern from the durable session log, kept out of it: events are
lightweight pointers (never the highlight/message payload), and a server restart resets `seq` and
drops in-flight events — safe because correctness rests on the durable session state plus the
agent's idempotent work predicate (a dropped notification only delays a card, never loses one).

Single-process, asyncio-only: no locks needed because there is no `await` between reading and
mutating shared state in any method (atomic under the event loop).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pydantic import BaseModel


class ActivityEvent(BaseModel):
    seq: int                           # global monotonic; the agent's wait offset
    kind: str                          # highlight_added | message_posted | lookup_opened
    session_id: str | None = None      # set for highlight_added / message_posted
    lookup_id: str | None = None       # set for lookup_opened
    query: str | None = None           # the lookup query, carried so the agent answers from the event
    created_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ActivityBroker:
    def __init__(self) -> None:
        self._events: list[ActivityEvent] = []
        self._seq = 0
        self._waiters: list[asyncio.Future] = []

    def publish(self, kind: str, *, session_id: str | None = None,
                lookup_id: str | None = None, query: str | None = None) -> ActivityEvent:
        self._seq += 1
        event = ActivityEvent(seq=self._seq, kind=kind, session_id=session_id,
                              lookup_id=lookup_id, query=query, created_at=_now())
        self._events.append(event)
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(event)
        self._waiters = []
        return event

    async def wait(self, since: int = 0, timeout: float | None = None) -> ActivityEvent | None:
        for event in self._events:
            if event.seq > since:
                return event
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._waiters.append(fut)
        try:
            return await (fut if timeout is None else asyncio.wait_for(fut, timeout))
        except asyncio.TimeoutError:
            if fut in self._waiters:
                self._waiters.remove(fut)
            return None
