"""Functional tests for the SessionActor + SessionManager (in-process, tmp store).

Covers the lifecycle and the live round-trip: AC-1, 2, 3, 4, 5, 9, 10.
"""
import asyncio
import pytest

from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight, EmitCard, EndSession
from review_mate.session.state import Origin, Side, LineRange, SessionStatus


@pytest.fixture
async def manager(tmp_path):
    m = SessionManager(root=tmp_path / "sessions")
    yield m
    await m.shutdown()


def _add(file="a.py", lo=1, hi=2):
    return AddHighlight(file=file, side=Side.NEW, line_range=LineRange(start=lo, end=hi))


async def test_create_session_is_active(manager):  # AC-1
    sid = await manager.create()
    actor = manager.get(sid)
    assert actor is not None
    assert actor.snapshot().status is SessionStatus.ACTIVE


async def test_unknown_session_returns_none(manager):  # AC-10
    assert manager.get("nope") is None


async def test_mutation_applies_and_pushes_to_subscriber(manager):  # AC-2
    sid = await manager.create()
    actor = manager.get(sid)
    stream = actor.subscribe(since=actor.snapshot().seq).__aiter__()
    res = await actor.submit(_add(), Origin.BROWSER)
    assert res.ok
    evt = await asyncio.wait_for(stream.__anext__(), 1)
    assert evt.type == "highlight_added"
    assert len(actor.snapshot().highlights) == 1


async def test_subscribe_replays_history_then_live(manager):  # AC-3
    sid = await manager.create()
    actor = manager.get(sid)
    await actor.submit(_add(file="past.py"), Origin.BROWSER)
    stream = actor.subscribe(since=0).__aiter__()
    # past events replayed (session_created + the highlight)
    types = [(await asyncio.wait_for(stream.__anext__(), 1)).type for _ in range(2)]
    assert "highlight_added" in types
    # then a live one
    await actor.submit(_add(file="live.py"), Origin.BROWSER)
    live = await asyncio.wait_for(stream.__anext__(), 1)
    assert live.type == "highlight_added"
    assert live.highlight.file == "live.py"


async def test_sessions_are_isolated(manager):  # AC-4
    a = manager.get(await manager.create())
    b = manager.get(await manager.create())
    await a.submit(_add(), Origin.BROWSER)
    assert len(a.snapshot().highlights) == 1
    assert len(b.snapshot().highlights) == 0


async def test_end_session_inactive_and_rejects_writes(manager):  # AC-5
    sid = await manager.create()
    actor = manager.get(sid)
    await manager.end(sid)
    assert actor.snapshot().status is SessionStatus.ENDED
    res = await actor.submit(_add(), Origin.BROWSER)
    assert not res.ok  # no mutation after end


async def test_authority_rejection_surfaces(manager):  # AC-9 (authority at the actor)
    sid = await manager.create()
    actor = manager.get(sid)
    res = await actor.submit(EmitCard(highlight_id="x", body="b"), Origin.BROWSER)
    assert not res.ok and res.reason


async def test_concurrent_writes_serialized_none_lost(manager):  # AC-9 (serialization)
    sid = await manager.create()
    actor = manager.get(sid)
    n = 50
    results = await asyncio.gather(
        *(actor.submit(_add(file=f"f{i}.py"), Origin.BROWSER) for i in range(n))
    )
    assert all(r.ok for r in results)
    seqs = sorted(r.seq for r in results)
    assert seqs == list(range(seqs[0], seqs[0] + n))  # contiguous, unique — nothing lost
    assert len(actor.snapshot().highlights) == n
