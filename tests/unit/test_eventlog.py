"""Unit tests for the append-only event log (JSONL persistence)."""
from review_mate.session.eventlog import EventLog
from review_mate.session import events as ev
from review_mate.session.state import Origin, Highlight, Side, LineRange


def _hl(i: str) -> ev.HighlightAdded:
    return ev.HighlightAdded(
        ts="t", origin=Origin.BROWSER,
        highlight=Highlight(id=i, file="a.py", side=Side.NEW,
                            line_range=LineRange(start=1, end=1), created_at="t"),
    )


def test_append_assigns_monotonic_seq_from_one(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    assert log.append(_hl("h1")) == 1
    assert log.append(_hl("h2")) == 2
    log.close()


def test_replay_returns_events_in_order(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append(_hl("h1"))
    log.append(_hl("h2"))
    log.close()
    got = list(EventLog(tmp_path / "events.jsonl").replay())
    assert [e.highlight.id for e in got] == ["h1", "h2"]
    assert [e.seq for e in got] == [1, 2]


def test_reopen_continues_seq_numbering(tmp_path):
    p = tmp_path / "events.jsonl"
    log = EventLog(p)
    log.append(_hl("h1"))
    log.close()
    log2 = EventLog(p)  # simulates a restart
    assert log2.append(_hl("h2")) == 2
    log2.close()


def test_data_is_durable_immediately_after_append(tmp_path):
    p = tmp_path / "events.jsonl"
    log = EventLog(p)
    log.append(_hl("h1"))
    # without closing, the line must already be on disk (flush+fsync)
    assert p.read_text().count("\n") == 1


def test_truncated_trailing_line_is_tolerated_on_replay(tmp_path):
    p = tmp_path / "events.jsonl"
    log = EventLog(p)
    log.append(_hl("h1"))
    log.append(_hl("h2"))
    log.close()
    # simulate a crash mid-append: a partial trailing line
    with p.open("a") as f:
        f.write('{"type": "highlight_added", "seq": 3, partial')
    got = list(EventLog(p).replay())
    assert [e.seq for e in got] == [1, 2]  # the un-acked partial event is dropped
