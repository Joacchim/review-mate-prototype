"""The view bus: topic building, subscription fan-out, coalescing and builder failure.

Pure logic — no transport, no manager. What a client is handed is decided here, so these are
the guarantees a second client gets to rely on.
"""
import asyncio

import pytest

from review_mate.view.bus import ViewBus
from review_mate.view.protocol import TopicError, TopicUpdate


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
        assert isinstance(msg, TopicUpdate)
        assert msg.topic == "demo" and msg.seq == 0 and msg.view == {"n": 1}


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


async def test_topic_nobody_watches_is_never_built_but_still_counts():
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


async def test_undelivered_updates_for_one_topic_coalesce():
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


async def test_unknown_topic_is_reported_not_raised():
    bus = bus_with(lambda: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["nope"])
        msg = await take(gen)
        assert isinstance(msg, TopicError) and msg.topic == "nope"
        assert "nope" not in sub.topics


async def test_a_failing_builder_reports_instead_of_killing_the_stream():
    async def builder():
        raise RuntimeError("host is down")

    bus = bus_with(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        msg = await take(gen)
        assert isinstance(msg, TopicError)
        assert "host is down" in msg.reason and "RuntimeError" in msg.reason
        assert "demo" in sub.topics           # still watching — a later publish can succeed


async def test_a_topic_error_does_not_supersede_a_pending_view():
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
        assert isinstance(first, TopicUpdate) and isinstance(second, TopicError)


async def test_closing_a_connection_ends_its_drain():
    bus = bus_with(lambda: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
    with pytest.raises(StopAsyncIteration):
        await take(gen)



# --- parameterised topic families -------------------------------------------

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
    edits = {"file:s1:a.py": 0, "file:s1:b.py": 0}

    async def builder(argument):
        topic = f"file:{argument}"
        return {"arg": argument, "edits": edits[topic]}

    bus = family_bus(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["file:s1:a.py", "file:s1:b.py"])
        await take(gen), await take(gen)
        edits["file:s1:a.py"] += 1
        await bus.publish("file:s1:a.py")
        msg = await take(gen)
        assert msg.topic == "file:s1:a.py" and msg.seq == 1
        await nothing_more(gen)              # b.py was not rebuilt, and did not advance


async def test_an_unregistered_family_is_still_an_unknown_topic():
    bus = family_bus(lambda a: _ready({}))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["diff:s1"])
        msg = await take(gen)
        assert isinstance(msg, TopicError) and msg.topic == "diff:s1"


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
    edits = {"n": 0}
    bus = family_bus(lambda a: _ready(dict(edits)))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["file:s1:a.py"])
        await take(gen)
        edits["n"] = 1
        await bus.publish("file:s1:a.py")
        assert (await take(gen)).seq == 1
        bus.unsubscribe(sub, ["file:s1:a.py"])
        bus.forget("file:s1:a.py")
        await bus.subscribe(sub, ["file:s1:a.py"])
        assert (await take(gen)).seq == 0


# --- pushing differences, not republishes ------------------------------------


async def test_a_view_that_rebuilt_identically_is_not_pushed():
    """The republish is coarse by design: a session rebuilds every topic it holds, and most of
    them are unchanged. A watcher already holding the view learns nothing from receiving it."""
    state = {"n": 1}
    bus = bus_with(lambda: _ready(dict(state)))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        await take(gen)
        await bus.publish("demo")
        await nothing_more(gen)


async def test_an_unchanged_rebuild_does_not_advance_seq():
    """seq counts changes to a topic, so an identical rebuild leaves nothing to have missed."""
    state = {"n": 1}
    bus = bus_with(lambda: _ready(dict(state)))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        await take(gen)
        await bus.publish("demo")
        state["n"] = 2
        await bus.publish("demo")
        assert (await take(gen)).seq == 1       # the real change is the first one counted


async def test_a_change_still_arrives_after_an_unchanged_rebuild():
    state = {"n": 1}
    bus = bus_with(lambda: _ready(dict(state)))
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        await take(gen)
        await bus.publish("demo")               # nothing to say
        state["n"] = 2
        await bus.publish("demo")
        assert (await take(gen)).view == {"n": 2}


async def test_a_repeated_failure_is_reported_once():
    async def builder():
        raise RuntimeError("host is down")

    bus = bus_with(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        assert isinstance(await take(gen), TopicError)
        await bus.publish("demo")
        await bus.publish("demo")
        first = await take(gen)                 # the subscribe-time error is not what watchers hold
        assert isinstance(first, TopicError)
        await nothing_more(gen)


async def test_a_view_after_an_error_is_pushed():
    """An error and a view never compare equal, whatever they carry."""
    fail = {"now": True}

    async def builder():
        if fail["now"]:
            raise RuntimeError("host is down")
        return {"reason": "host is down"}       # the same words, as a view

    bus = bus_with(builder)
    async with bus.connect() as sub:
        gen = sub.drain()
        await bus.subscribe(sub, ["demo"])
        await take(gen)
        await bus.publish("demo")
        assert isinstance(await take(gen), TopicError)
        fail["now"] = False
        await bus.publish("demo")
        recovered = await take(gen)
        assert isinstance(recovered, TopicUpdate) and recovered.view == {"reason": "host is down"}


async def test_an_unwatched_republish_leaves_nothing_held():
    """Whether an unwatched topic changed is unknowable without building it, so the next watcher
    is sent the view rather than compared against one nobody holds."""
    state = {"n": 1}
    bus = bus_with(lambda: _ready(dict(state)))
    async with bus.connect() as first:
        gen = first.drain()
        await bus.subscribe(first, ["demo"])
        await take(gen)
        bus.unsubscribe(first, ["demo"])
        await bus.publish("demo")               # nobody watching
    async with bus.connect() as second:
        gen = second.drain()
        await bus.subscribe(second, ["demo"])
        assert (await take(gen)).view == {"n": 1}
        state["n"] = 2
        await bus.publish("demo")
        assert (await take(gen)).view == {"n": 2}


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
