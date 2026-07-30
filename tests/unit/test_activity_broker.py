"""Unit tests for the ActivityBroker — the global ephemeral notification spine (review-fleet).

Mirrors LookupBroker's contract: a single monotonic seq, publish wakes long-poll waiters, wait
returns the next event past `since` or None on timeout.
"""
import asyncio

from review_mate.activity.broker import ActivityBroker, ActivityEvent


def test_publish_assigns_monotonic_seq_across_kinds():
    b = ActivityBroker()
    e1 = b.publish("context_requested", session_id="s1")
    e2 = b.publish("message_posted", session_id="s2")
    e3 = b.publish("lookup_opened", lookup_id="l1", query="the cache MR")
    assert [e1.seq, e2.seq, e3.seq] == [1, 2, 3]
    assert isinstance(e1, ActivityEvent)
    assert e1.kind == "context_requested" and e1.session_id == "s1"
    assert e3.lookup_id == "l1" and e3.query == "the cache MR" and e3.session_id is None


async def test_wait_returns_existing_event_past_since_immediately():
    b = ActivityBroker()
    b.publish("context_requested", session_id="s1")  # seq 1
    ev = await b.wait(since=0, timeout=1)
    assert ev is not None and ev.seq == 1


async def test_wait_skips_events_at_or_below_since():
    b = ActivityBroker()
    b.publish("context_requested", session_id="s1")   # seq 1
    b.publish("context_requested", session_id="s2")   # seq 2
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


# --- watcher presence: what the reviewer's UI reads to tell "working" from "nobody home" ---

def test_watcher_reports_nothing_attached_before_anyone_polls():
    b = ActivityBroker()
    assert b.watcher() == {"attached": False, "parked": False, "last_seen": None}


async def test_watcher_is_parked_while_a_wait_is_in_flight():
    b = ActivityBroker()
    task = asyncio.create_task(b.wait(since=0, timeout=1))
    await asyncio.sleep(0.01)                      # let the wait park
    assert b.watcher()["parked"] is True and b.watcher()["attached"] is True
    b.publish("message_posted", session_id="s1")
    await task


async def test_watcher_stays_attached_between_two_polls():
    """A watcher that just took an event isn't parked for the moment it takes to re-poll — it is
    still there, and the UI must not flip to "nobody is listening" in that gap."""
    b = ActivityBroker()
    b.publish("message_posted", session_id="s1")
    assert await b.wait(since=0, timeout=1) is not None    # returns at once, parks no future
    status = b.watcher()
    assert status["parked"] is False and status["attached"] is True and status["last_seen"]


async def test_watcher_goes_unattached_once_the_last_poll_ages_out():
    from datetime import datetime, timedelta, timezone

    from review_mate.activity.broker import WATCHER_TTL

    b = ActivityBroker()
    assert await b.wait(since=0, timeout=0.02) is None
    b._last_wait_at = datetime.now(timezone.utc) - timedelta(seconds=WATCHER_TTL + 1)
    assert b.watcher()["attached"] is False
