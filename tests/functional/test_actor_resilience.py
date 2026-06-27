"""Resilience of the session actor: slow-consumer drop and the writer-death guard."""
import asyncio
import pytest

from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight
from review_mate.session.state import Origin, Side, LineRange


@pytest.fixture
async def manager(tmp_path):
    m = SessionManager(root=tmp_path / "sessions")
    yield m
    await m.shutdown()


def _add(i):
    return AddHighlight(file=f"f{i}.py", side=Side.NEW, line_range=LineRange(start=1, end=1))


async def test_slow_consumer_is_dropped_not_hung(manager, monkeypatch):
    monkeypatch.setattr("review_mate.session.actor._SUBSCRIBER_BUFFER", 10)  # keep the test fast
    sid = await manager.create()
    actor = manager.get(sid)
    gen = actor.subscribe(since=0).__aiter__()
    await gen.__anext__()  # session_created — registers the subscriber, then suspends on live get()

    # flood past the per-subscriber buffer without consuming → the subscriber is dropped
    n = 40
    for i in range(n):
        await actor.submit(_add(i), Origin.BROWSER)

    count = 0
    with pytest.raises(StopAsyncIteration):
        while True:
            await asyncio.wait_for(gen.__anext__(), 1)
            count += 1
    assert count < n  # the stream terminated (dropped) rather than hanging or buffering all


async def test_submit_raises_when_writer_is_dead(manager):
    actor = manager.get(await manager.create())
    actor._dead = RuntimeError("boom")  # simulate a crashed writer task
    with pytest.raises(RuntimeError):
        await actor.submit(_add(0), Origin.BROWSER)
