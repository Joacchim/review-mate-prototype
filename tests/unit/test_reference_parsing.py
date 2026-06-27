"""Unit tests for pure MR reference parsing (AC-2,3,4)."""
from review_mate.host.base import parse_reference


def test_parses_full_mr_url():
    ref = parse_reference("https://gitlab.com/group/sub/proj/-/merge_requests/42", "default.host")
    assert ref is not None
    assert ref.host == "gitlab.com"
    assert ref.project == "group/sub/proj"
    assert ref.iid == 42


def test_parses_shorthand():
    ref = parse_reference("group/sub/proj!7", "gitlab.example.com")
    assert ref is not None
    assert ref.host == "gitlab.example.com"
    assert ref.project == "group/sub/proj"
    assert ref.iid == 7


def test_unparseable_returns_none():
    assert parse_reference("just some text", "h") is None
    assert parse_reference("", "h") is None
    assert parse_reference("group/proj!notanumber", "h") is None
