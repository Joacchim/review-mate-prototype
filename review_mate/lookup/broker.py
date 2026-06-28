"""LookupBroker — the standing channel that lets the reviewer ask the agent to find an MR.

MR *discovery* is a different concern from a review *session*: it happens before any session
exists (on the landing page) and is ephemeral — there is nothing to replay or resume. So it lives
here, outside the event-sourced session model, as an in-process pub/sub rather than polluting the
durable log. The browser opens a request; the agent (watching) answers; the browser long-polls for
that answer. If no agent is attached, the answer simply never comes and the UI falls back to host
search.

Single-process, asyncio-only: no locks needed because there is no `await` between reading and
mutating shared state in any method (atomic under the event loop).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class LookupRequest(BaseModel):
    id: str
    seq: int                       # monotonic; the agent's wait offset
    query: str
    status: str = "pending"        # pending | answered
    answer: str = ""               # the agent's prose ("probably !573, the cache MR")
    candidates: list[dict] = Field(default_factory=list)  # loadable {host, project, iid, title, url}
    created_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LookupBroker:
    def __init__(self) -> None:
        self._by_id: dict[str, LookupRequest] = {}
        self._by_seq: list[LookupRequest] = []
        self._seq = 0
        self._req_waiters: list[asyncio.Future] = []          # agent awaiting a new request
        self._ans_waiters: dict[str, list[asyncio.Future]] = {}  # browser awaiting an answer

    # --- browser side -------------------------------------------------------

    def create(self, query: str) -> LookupRequest:
        self._seq += 1
        req = LookupRequest(id=uuid.uuid4().hex, seq=self._seq, query=query, created_at=_now())
        self._by_id[req.id] = req
        self._by_seq.append(req)
        for fut in self._req_waiters:
            if not fut.done():
                fut.set_result(req)
        self._req_waiters = []
        return req

    def get(self, lookup_id: str) -> LookupRequest | None:
        return self._by_id.get(lookup_id)

    async def wait_for_answer(self, lookup_id: str, timeout: float | None = None) -> LookupRequest | None:
        req = self._by_id.get(lookup_id)
        if req is None:
            return None
        if req.status == "answered":
            return req
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._ans_waiters.setdefault(lookup_id, []).append(fut)
        try:
            return await (fut if timeout is None else asyncio.wait_for(fut, timeout))
        except asyncio.TimeoutError:
            return req  # still pending — let the caller re-poll or fall back

    # --- agent side ---------------------------------------------------------

    async def wait_for_request(self, since: int = 0, timeout: float | None = None) -> LookupRequest | None:
        for req in self._by_seq:
            if req.seq > since:
                return req
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._req_waiters.append(fut)
        try:
            return await (fut if timeout is None else asyncio.wait_for(fut, timeout))
        except asyncio.TimeoutError:
            if fut in self._req_waiters:
                self._req_waiters.remove(fut)
            return None

    def answer(self, lookup_id: str, answer: str, candidates: list[dict] | None = None) -> LookupRequest:
        req = self._by_id.get(lookup_id)
        if req is None:
            raise KeyError(lookup_id)
        req.answer = answer
        req.candidates = candidates or []
        req.status = "answered"
        for fut in self._ans_waiters.pop(lookup_id, []):
            if not fut.done():
                fut.set_result(req)
        return req
