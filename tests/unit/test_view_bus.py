"""The view bus: scope building, subscription fan-out, coalescing and builder failure.

Pure logic — no transport, no manager. What a client is handed is decided here, so these are
the guarantees a second client gets to rely on.
"""
import asyncio

import pytest

from review_mate.view.bus import ViewBus
from review_mate.view.protocol import ScopeError, ScopeUpdate


async def _ready(value):
    return value


async def take(gen, timeout=1.0):
    return await asyncio.wait_for(gen.__anext__(), timeout)


async def nothing_more(gen, timeout=0.05):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(gen.__anext__(), timeout)


def bus_with(value_source):
    bus = ViewBus()
    bus.register("demo", value_source)
    return bus


async def test_subscribe_delivers_the_current_view_without_advancing_seq():
    bus = bus_with(lambda: _ready({"n": 1}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        msg = await take(gen)
        assert isinstance(msg, ScopeUpdate)
        assert msg.scope == "demo" and msg.seq == 0 and msg.view == {"n": 1}


async def test_publish_advances_seq_and_fans_out():
    state = {"n": 1}
    bus = bus_with(lambda: _ready(dict(state)))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        await take(gen)
        state["n"] = 2
        await bus.publish("demo")
        msg = await take(gen)
        assert msg.seq == 1 and msg.view == {"n": 2}


async def test_scope_nobody_watches_is_never_built_but_still_counts():
    builds = []

    async def builder():
        builds.append(1)
        return {"n": len(builds)}

    bus = bus_with(builder)
    await bus.publish("demo")
    await bus.publish("demo")
    assert builds == []                       # nothing watching → nothing built
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        msg = await take(gen)
        assert msg.seq == 2                   # but the client learns how current its first view is


async def test_undelivered_updates_for_one_scope_coalesce():
    state = {"n": 0}
    bus = bus_with(lambda: _ready(dict(state)))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        await take(gen)
        for n in (1, 2, 3):
            state["n"] = n
            await bus.publish("demo")
        msg = await take(gen)
        assert msg.view == {"n": 3} and msg.seq == 3
        await nothing_more(gen)               # the superseded two were never queued


async def test_unsubscribe_stops_delivery():
    bus = bus_with(lambda: _ready({"n": 1}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        await take(gen)
        bus.unsubscribe(sub, ["demo"])
        await bus.publish("demo")
        await nothing_more(gen)


async def test_unknown_scope_is_reported_not_raised():
    bus = bus_with(lambda: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["nope"])
        msg = await take(gen)
        assert isinstance(msg, ScopeError) and msg.scope == "nope"
        assert "nope" not in sub.scopes


async def test_a_failing_builder_reports_instead_of_killing_the_stream():
    async def builder():
        raise RuntimeError("host is down")

    bus = bus_with(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        msg = await take(gen)
        assert isinstance(msg, ScopeError)
        assert "host is down" in msg.reason and "RuntimeError" in msg.reason
        assert "demo" in sub.scopes           # still watching — a later publish can succeed


async def test_a_scope_error_does_not_supersede_a_pending_view():
    calls = []

    async def builder():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("transient")
        return {"n": len(calls)}

    bus = bus_with(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])       # build 1 → view
        await bus.publish("demo")                # build 2 → error
        first, second = await take(gen), await take(gen)
        assert isinstance(first, ScopeUpdate) and isinstance(second, ScopeError)


async def test_closing_a_connection_ends_its_drain():
    bus = bus_with(lambda: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
    with pytest.raises(StopAsyncIteration):
        await take(gen)

