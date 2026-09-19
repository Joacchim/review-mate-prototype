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



# --- parameterised scope families -------------------------------------------

def family_bus(builder):
    bus = ViewBus()
    bus.register_family("file", builder)
    return bus


async def test_a_family_serves_any_member_from_one_registration():
    async def builder(argument):
        return {"arg": argument}

    bus = family_bus(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["file:s1:a.py", "file:s1:pkg/b.py"])
        first, second = await take(gen), await take(gen)
        assert {first.view["arg"], second.view["arg"]} == {"s1:a.py", "s1:pkg/b.py"}


async def test_family_members_carry_independent_seq():
    async def builder(argument):
        return {"arg": argument}

    bus = family_bus(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["file:s1:a.py", "file:s1:b.py"])
        await take(gen), await take(gen)
        await bus.publish("file:s1:a.py")
        msg = await take(gen)
        assert msg.scope == "file:s1:a.py" and msg.seq == 1
        await nothing_more(gen)              # b.py was not rebuilt, and did not advance


async def test_an_unregistered_family_is_still_an_unknown_scope():
    bus = family_bus(lambda a: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["diff:s1"])
        msg = await take(gen)
        assert isinstance(msg, ScopeError) and msg.scope == "diff:s1"


async def test_watched_finds_the_members_to_republish():
    bus = family_bus(lambda a: _ready({}))
    bus.register("hub", lambda: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["hub", "file:s1:a.py", "file:s2:z.py"])
        for _ in range(3):
            await take(gen)
        assert bus.watched("file:s1:") == {"file:s1:a.py"}
        assert bus.watched("file:") == {"file:s1:a.py", "file:s2:z.py"}


async def test_forget_resets_a_members_sequence():
    bus = family_bus(lambda a: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["file:s1:a.py"])
        await take(gen)
        await bus.publish("file:s1:a.py")
        assert (await take(gen)).seq == 1
        bus.unsubscribe(sub, ["file:s1:a.py"])
        bus.forget("file:s1:a.py")
        await bus.subscribe(sub, ["file:s1:a.py"])
        assert (await take(gen)).seq == 0


# --- watch lifecycle ---------------------------------------------------------

def hooked_bus():
    events = []
    bus = ViewBus(on_first_watch=lambda s: events.append(("first", s)),
                  on_last_watch=lambda s: events.append(("last", s)))
    bus.register("demo", lambda: _ready({}))
    return bus, events


async def test_the_first_watcher_and_the_last_are_announced():
    bus, events = hooked_bus()
    async with bus.connect() as sub:
        await bus.subscribe(sub, ["demo"])
        assert events == [("first", "demo")]
        bus.unsubscribe(sub, ["demo"])
        assert events == [("first", "demo"), ("last", "demo")]


async def test_a_second_watcher_does_not_re_announce():
    bus, events = hooked_bus()
    async with bus.connect() as one, bus.connect() as two:
        await bus.subscribe(one, ["demo"])
        await bus.subscribe(two, ["demo"])
        assert events == [("first", "demo")]          # one announcement, not two
        bus.unsubscribe(one, ["demo"])
        assert events == [("first", "demo")]          # still watched by the other
        bus.unsubscribe(two, ["demo"])
        assert events[-1] == ("last", "demo")


async def test_a_dropped_connection_releases_its_watches():
    bus, events = hooked_bus()
    async with bus.connect() as sub:
        await bus.subscribe(sub, ["demo"])
    assert events[-1] == ("last", "demo")


async def test_unsubscribing_something_not_watched_announces_nothing():
    bus, events = hooked_bus()
    async with bus.connect() as sub:
        bus.unsubscribe(sub, ["demo"])
        assert events == []
