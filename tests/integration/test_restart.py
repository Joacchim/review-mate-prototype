"""Integration: durability across a real restart, on real disk. Covers AC-7, AC-8."""
import pytest

from review_mate.session.manager import SessionManager
from review_mate.session.commands import AddHighlight
from review_mate.session.state import Origin, Side, LineRange, SessionStatus


def _add(file):
    return AddHighlight(file=file, side=Side.NEW, line_range=LineRange(start=1, end=1))


async def test_state_restored_after_restart(tmp_path):  # AC-7, AC-8
    root = tmp_path / "sessions"
    m1 = SessionManager(root=root)
    sid = await m1.create()
    await m1.get(sid).submit(_add("kept.py"), Origin.BROWSER)
    last_seq = m1.get(sid).snapshot().seq
    await m1.shutdown()  # process goes away

    m2 = SessionManager(root=root)  # fresh process
    await m2.restore_all()
    actor = m2.get(sid)
    assert actor is not None
    snap = actor.snapshot()
    assert snap.status is SessionStatus.ACTIVE
    assert [h.file for h in snap.highlights] == ["kept.py"]
    assert snap.seq == last_seq
    await m2.shutdown()


async def test_subscribe_resumes_from_offset_after_restart(tmp_path):  # AC-8
    root = tmp_path / "sessions"
    m1 = SessionManager(root=root)
    sid = await m1.create()
    await m1.get(sid).submit(_add("one.py"), Origin.BROWSER)
    offset = m1.get(sid).snapshot().seq
    await m1.get(sid).submit(_add("two.py"), Origin.BROWSER)
    await m1.shutdown()

    m2 = SessionManager(root=root)
    await m2.restore_all()
    actor = m2.get(sid)
    stream = actor.subscribe(since=offset).__aiter__()
    import asyncio
    evt = await asyncio.wait_for(stream.__anext__(), 1)
    assert evt.highlight.file == "two.py"  # only events after the offset
    await m2.shutdown()


async def test_restart_tolerates_corrupt_trailing_line(tmp_path):  # AC-7/8 + resilience
    root = tmp_path / "sessions"
    m1 = SessionManager(root=root)
    sid = await m1.create()
    await m1.get(sid).submit(_add("kept.py"), Origin.BROWSER)
    last_seq = m1.get(sid).snapshot().seq
    await m1.shutdown()
    # simulate a crash mid-append: a partial trailing line
    with (root / sid / "events.jsonl").open("a") as f:
        f.write('{"type":"highlight_added","seq":99,partial')

    m2 = SessionManager(root=root)
    await m2.restore_all()
    snap = m2.get(sid).snapshot()
    assert [h.file for h in snap.highlights] == ["kept.py"] and snap.seq == last_seq
    await m2.shutdown()


async def test_restart_skips_a_corrupt_session_and_boots_the_rest(tmp_path):
    root = tmp_path / "sessions"
    m1 = SessionManager(root=root)
    good = await m1.create()
    bad = await m1.create()
    await m1.get(good).submit(_add("good.py"), Origin.BROWSER)
    await m1.shutdown()
    # corrupt a NON-trailing line of `bad` (real mid-file corruption → that session is unrestorable)
    log = (root / bad / "events.jsonl")
    lines = log.read_text().splitlines()
    lines.insert(0, "{ this is not valid json")
    log.write_text("\n".join(lines) + "\n")

    m2 = SessionManager(root=root)
    await m2.restore_all()  # must not raise — one bad session can't stop the boot
    assert m2.get(good) is not None
    assert m2.get(bad) is None  # skipped
    await m2.shutdown()


async def test_list_reflects_persisted_sessions(tmp_path):
    root = tmp_path / "sessions"
    m1 = SessionManager(root=root)
    sid = await m1.create()
    await m1.shutdown()
    m2 = SessionManager(root=root)
    await m2.restore_all()
    assert [s.id for s in m2.list()] == [sid]
    await m2.shutdown()
