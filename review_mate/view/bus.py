"""The view bus: builds scopes on demand and fans whole-scope replacements out to subscribers.

A scope is produced by a registered builder — an async callable returning a JSON-ready dict —
and pushed to whoever subscribed to it. Nothing is built for a scope nobody is watching, so an
expensive scope added later costs nothing until a client asks for it.

Whole-scope replacement makes the slow-consumer policy trivial: a queued update for a scope is
worthless once a newer one exists, so a subscriber holds at most one pending message per scope
and the newest always wins. No unbounded queue, no backpressure onto the publisher.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from review_mate.view.protocol import Message, ScopeError, ScopeUpdate

Builder = Callable[[], Awaitable[dict]]
FamilyBuilder = Callable[[str], Awaitable[dict]]

# A scope name is either a singleton ("hub") or a family member ("file:<sid>:<path>"): the kind up
# to the first colon selects the builder, and everything after it is that builder's argument.
FAMILY_SEP = ":"


class Subscription:
    """One client connection's view of the bus: the scopes it watches and its pending messages."""

    def __init__(self) -> None:
        self.scopes: set[str] = set()
        self._pending: dict[str, Message] = {}
        self._wake = asyncio.Event()
        self._closed = False

    def offer(self, message: Message) -> None:
        """Queue a message, superseding any undelivered one for the same scope."""
        if self._closed:
            return
        self._pending[message.key] = message
        self._wake.set()

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    async def drain(self) -> AsyncIterator[Message]:
        """Yield messages as they arrive, until the subscription closes."""
        while True:
            while self._pending:
                key = next(iter(self._pending))      # FIFO across scopes; newest wins within one
                yield self._pending.pop(key)
            if self._closed:
                return
            await self._wake.wait()
            self._wake.clear()


class ViewBus:
    def __init__(self) -> None:
        self._builders: dict[str, Builder] = {}
        self._families: dict[str, FamilyBuilder] = {}
        self._seq: dict[str, int] = {}
        self._subs: set[Subscription] = set()

    def register(self, scope: str, builder: Builder) -> None:
        """A singleton scope, addressed by its exact name."""
        self._builders[scope] = builder
        self._seq.setdefault(scope, 0)

    def register_family(self, kind: str, builder: FamilyBuilder) -> None:
        """A parameterised scope. `kind` selects the builder and receives the rest of the name,
        so one registration serves every `kind:<argument>` a client cares to subscribe to."""
        self._families[kind] = builder

    def _resolve(self, scope: str) -> Builder | None:
        if scope in self._builders:
            return self._builders[scope]
        kind, sep, argument = scope.partition(FAMILY_SEP)
        if sep and kind in self._families:
            family = self._families[kind]
            return lambda: family(argument)
        return None

    @property
    def scopes(self) -> set[str]:
        """The singleton scopes."""
        return set(self._builders)

    @property
    def families(self) -> set[str]:
        """The registered family kinds — the part of the surface a scope name's prefix selects."""
        return set(self._families)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Subscription]:
        sub = Subscription()
        self._subs.add(sub)
        try:
            yield sub
        finally:
            sub.close()
            self._subs.discard(sub)

    async def subscribe(self, sub: Subscription, scopes: list[str]) -> None:
        """Add scopes to a subscription and send each one's current view immediately.

        The initial send does not bump `seq`: subscribing is not a change to the scope.
        """
        for scope in scopes:
            if self._resolve(scope) is None:
                sub.offer(ScopeError(scope=scope, reason="unknown scope"))
                continue
            sub.scopes.add(scope)
            self._seq.setdefault(scope, 0)
            sub.offer(await self._render(scope))

    def unsubscribe(self, sub: Subscription, scopes: list[str]) -> None:
        for scope in scopes:
            sub.scopes.discard(scope)

    def forget(self, scope: str) -> None:
        """Drop a family member's sequence once its subject is gone, so a reused name starts clean."""
        self._seq.pop(scope, None)

    def watched(self, prefix: str) -> set[str]:
        """Every scope currently subscribed whose name starts with `prefix` — what to republish
        when the thing behind a family of scopes changes."""
        return {s for sub in self._subs for s in sub.scopes if s.startswith(prefix)}

    async def publish(self, scope: str) -> None:
        """Rebuild a scope and push it to every subscriber watching it.

        The build is skipped when nobody is watching, but `seq` still advances — it counts
        changes to the scope, not deliveries of it, so a client that subscribes later can tell
        how current its first view is.
        """
        if self._resolve(scope) is None:
            return
        self._seq[scope] = self._seq.get(scope, 0) + 1
        watchers = [s for s in self._subs if scope in s.scopes]
        if not watchers:
            return
        message = await self._render(scope)
        for sub in watchers:
            sub.offer(message)

    async def _render(self, scope: str) -> Message:
        """Build a scope into a message. A builder failure is reported, never raised at the client."""
        builder = self._resolve(scope)
        if builder is None:   # unregistered between resolution and render
            return ScopeError(scope=scope, reason="unknown scope")
        try:
            view = await builder()
        except Exception as exc:   # a broken builder must not take the connection down with it
            return ScopeError(scope=scope, reason=f"{type(exc).__name__}: {exc}")
        return ScopeUpdate(scope=scope, seq=self._seq.get(scope, 0), view=view)
