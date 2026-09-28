"""The view bus: builds topics on demand and fans whole-topic replacements out to subscribers.

A topic is produced by a registered builder — an async callable returning a JSON-ready dict —
and pushed to whoever subscribed to it. Nothing is built for a topic nobody is watching, so an
expensive topic added later costs nothing until a client asks for it.

Whole-topic replacement makes the slow-consumer policy trivial: a queued update for a topic is
worthless once a newer one exists, so a subscriber holds at most one pending message per topic
and the newest always wins. No unbounded queue, no backpressure onto the publisher.

It also means a republish carries no information when the view did not change, and republishing is
coarse by design: a session's reading topics are rebuilt together because what a change touches is
not worth tracking per event. So the bus compares a rendered view against what its watchers already
hold and pushes only a difference. A tokenized file is the frame that pays for this — it dwarfs
every other topic, and a highlight or a chat message leaves it untouched.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from review_mate.view.protocol import Message, TopicError, TopicUpdate

Builder = Callable[[], Awaitable[dict]]
FamilyBuilder = Callable[[str], Awaitable[dict]]

# A topic name is either a singleton ("hub") or a family member ("file:<sid>:<path>"): the kind up
# to the first colon selects the builder, and everything after it is that builder's argument.
FAMILY_SEP = ":"


def _fingerprint(kind: str, payload) -> str:
    """What a watcher holds, as a digest. `kind` keeps an error from matching a view."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{kind}:{body}".encode()).hexdigest()


class Subscription:
    """One client connection's view of the bus: the topics it watches and its pending messages."""

    def __init__(self) -> None:
        self.topics: set[str] = set()
        self._pending: dict[str, Message] = {}
        self._wake = asyncio.Event()
        self._closed = False

    def offer(self, message: Message) -> None:
        """Queue a message, superseding any undelivered one for the same topic."""
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
                key = next(iter(self._pending))      # FIFO across topics; newest wins within one
                yield self._pending.pop(key)
            if self._closed:
                return
            await self._wake.wait()
            self._wake.clear()


class ViewBus:
    def __init__(self, on_first_watch=None, on_last_watch=None) -> None:
        self._builders: dict[str, Builder] = {}
        self._families: dict[str, FamilyBuilder] = {}
        self._seq: dict[str, int] = {}
        self._sent: dict[str, str] = {}      # topic -> fingerprint of what watchers hold
        self._subs: set[Subscription] = set()
        # Called as a topic gains its first watcher and loses its last. Work that only makes sense
        # while someone is looking — tailing a session's events to know when to republish — starts
        # and stops here rather than running for every session the server holds.
        self._on_first_watch = on_first_watch
        self._on_last_watch = on_last_watch

    def watchers(self, topic: str) -> int:
        return sum(1 for sub in self._subs if topic in sub.topics)

    def _note_watched(self, topic: str) -> None:
        if self._on_first_watch is not None and self.watchers(topic) == 1:
            self._on_first_watch(topic)

    def _note_unwatched(self, topic: str) -> None:
        if self._on_last_watch is not None and self.watchers(topic) == 0:
            self._on_last_watch(topic)

    def register(self, topic: str, builder: Builder) -> None:
        """A singleton topic, addressed by its exact name."""
        self._builders[topic] = builder
        self._seq.setdefault(topic, 0)

    def register_family(self, kind: str, builder: FamilyBuilder) -> None:
        """A parameterised topic. `kind` selects the builder and receives the rest of the name,
        so one registration serves every `kind:<argument>` a client cares to subscribe to."""
        self._families[kind] = builder

    def _resolve(self, topic: str) -> Builder | None:
        if topic in self._builders:
            return self._builders[topic]
        kind, sep, argument = topic.partition(FAMILY_SEP)
        if sep and kind in self._families:
            family = self._families[kind]
            return lambda: family(argument)
        return None

    @property
    def topics(self) -> set[str]:
        """The singleton topics."""
        return set(self._builders)

    @property
    def families(self) -> set[str]:
        """The registered family kinds — the part of the surface a topic name's prefix selects."""
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
            for topic in sub.topics:     # a dropped connection releases its watches too
                self._note_unwatched(topic)

    async def subscribe(self, sub: Subscription, topics: list[str]) -> None:
        """Add topics to a subscription and send each one's current view immediately.

        The initial send does not bump `seq`: subscribing is not a change to the topic.
        """
        for topic in topics:
            if self._resolve(topic) is None:
                sub.offer(TopicError(topic=topic, reason="unknown topic"))
                continue
            sub.topics.add(topic)
            self._seq.setdefault(topic, 0)
            self._note_watched(topic)
            sub.offer(await self._render(topic))

    def unsubscribe(self, sub: Subscription, topics: list[str]) -> None:
        for topic in topics:
            if topic in sub.topics:
                sub.topics.discard(topic)
                self._note_unwatched(topic)

    def forget(self, topic: str) -> None:
        """Drop a family member's sequence once its subject is gone, so a reused name starts clean."""
        self._seq.pop(topic, None)
        self._sent.pop(topic, None)

    def watched(self, prefix: str) -> set[str]:
        """Every topic currently subscribed whose name starts with `prefix` — what to republish
        when the thing behind a family of topics changes."""
        return {s for sub in self._subs for s in sub.topics if s.startswith(prefix)}

    async def publish(self, topic: str) -> None:
        """Rebuild a topic and push it to every subscriber watching it, if it changed.

        `seq` counts changes to the topic rather than deliveries of it, so a view that rebuilt
        identically advances nothing: there is no change for a later subscriber to have missed.

        The build is skipped when nobody is watching, and `seq` advances on faith there — whether
        the view changed is unknowable without building it, and a client that subscribes later
        should read its first view as "not necessarily the first version".
        """
        if self._resolve(topic) is None:
            return
        watchers = [s for s in self._subs if topic in s.topics]
        if not watchers:
            self._seq[topic] = self._seq.get(topic, 0) + 1
            self._sent.pop(topic, None)      # nothing was compared, so nothing is held
            return
        built = await self._build(topic)
        held = (_fingerprint("error", built.reason) if isinstance(built, TopicError)
                else _fingerprint("view", built))
        if held == self._sent.get(topic):
            return                           # the watchers already hold this exact view
        self._seq[topic] = self._seq.get(topic, 0) + 1
        self._sent[topic] = held
        message = (built if isinstance(built, TopicError)
                   else TopicUpdate(topic=topic, seq=self._seq[topic], view=built))
        for sub in watchers:
            sub.offer(message)

    async def _build(self, topic: str):
        """The topic's view, or the error that stands in for it — never a raise at the client."""
        builder = self._resolve(topic)
        if builder is None:   # unregistered between resolution and render
            return TopicError(topic=topic, reason="unknown topic")
        try:
            return await builder()
        except Exception as exc:   # a broken builder must not take the connection down with it
            return TopicError(topic=topic, reason=f"{type(exc).__name__}: {exc}")

    async def _render(self, topic: str) -> Message:
        """Build a topic into a message, and record it as what a watcher now holds."""
        built = await self._build(topic)
        if isinstance(built, TopicError):
            return built
        self._sent[topic] = _fingerprint("view", built)
        return TopicUpdate(topic=topic, seq=self._seq.get(topic, 0), view=built)
