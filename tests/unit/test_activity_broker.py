"""Unit tests for the ActivityBroker — the global ephemeral notification spine (review-fleet).

Mirrors LookupBroker's contract: a single monotonic seq, publish wakes long-poll waiters, wait
returns the next event past `since` or None on timeout.
"""
import asyncio

from review_mate.activity.broker import ActivityBroker, ActivityEvent


def test_publish_assigns_monotonic_seq_across_kinds():
    b = ActivityBroker()
    e1 = b.publish("highlight_added", session_id="s1")
    e2 = b.publish("message_posted", session_id="s2")
    e3 = b.publish("lookup_opened", lookup_id="l1", query="the cache MR")
    assert [e1.seq, e2.seq, e3.seq] == [1, 2, 3]
    assert isinstance(e1, ActivityEvent)
    assert e1.kind == "highlight_added" and e1.session_id == "s1"
    assert e3.lookup_id == "l1" and e3.query == "the cache MR" and e3.session_id is None


async def test_wait_returns_existing_event_past_since_immediately():
    b = ActivityBroker()
    b.publish("highlight_added", session_id="s1")  # seq 1
    ev = await b.wait(since=0, timeout=1)
    assert ev is not None and ev.seq == 1


async def test_wait_skips_events_at_or_below_since():
    b = ActivityBroker()
    b.publish("highlight_added", session_id="s1")   # seq 1
    b.publish("highlight_added", session_id="s2")   # seq 2
    ev = await b.wait(since=1, timeout=1)
    assert ev is not None and ev.seq == 2 and ev.session_id == "s2"


async def test_wait_parks_then_resolves_on_publish():
    b = ActivityBroker()

    async def publish_soon():
        await asyncio.sleep(0.01)
        b.publish("message_posted", session_id="s9")

    task = asyncio.create_task(publish_soon())
    ev = await b.wait(since=0, timeout=1)
    assert ev is not None and ev.kind == "message_posted" and ev.session_id == "s9"
    await task


async def test_wait_times_out_to_none():
    b = ActivityBroker()
    ev = await b.wait(since=0, timeout=0.02)
    assert ev is None
