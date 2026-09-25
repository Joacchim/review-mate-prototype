"""Reading a change by what matters, not by the order Claude happened to find things in.

Forty findings in one list is a list nobody reads past the top of. A label is only worth carrying
if it changes what the reviewer sees first, so what is tested here is the ordering and the filter —
and the reviewer's ability to disagree, because a wrong label costs attention on every scan.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.state import Card, Criticality, Label, Theme

from webui.fixtures.scenarios import two_file_review
from webui.pages.detail import DetailPage
from webui.pages.diff import DiffPage
from webui.pages.rail import RailPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def rail(page) -> RailPage:
    return RailPage(page)


@pytest.fixture
def detail(page) -> DetailPage:
    return DetailPage(page)


def _label(theme, crit, about="", by="agent"):
    from review_mate.session.state import Origin
    return Label(theme=Theme(theme), criticality=Criticality(crit), about=about, by=Origin(by))


def _with_insights(*specs):
    """One MR-level card per spec: (body, label or None)."""
    state = two_file_review("s1")
    state.cards = [Card(id=f"c{i}", highlight_id=None, body=body, label=label,
                        created_at=f"2026-01-01T00:0{i}:00+00:00")
                   for i, (body, label) in enumerate(specs)]
    return state


def test_the_worst_thing_is_first(diff, rail, staged):
    staged.put(_with_insights(
        ("a naming preference", _label("naming", "low")),
        ("an unbounded retry", _label("bug", "high")),
        ("a slow path", _label("performance", "medium")),
    ))
    diff.load("s1")
    expect(rail.insight_labels).to_have_text(
        ["bug · high", "performance · medium", "naming · low"])


def test_an_unclassified_finding_sorts_last_not_lowest(diff, rail, staged):
    """Nobody classified it. Burying it under the lows would make that decision for them."""
    staged.put(_with_insights(
        ("nobody looked at this one", None),
        ("a naming preference", _label("naming", "low")),
    ))
    diff.load("s1")
    expect(rail.pinned_insights.first).to_contain_text("a naming preference")
    expect(rail.pinned_insights.last).to_contain_text("nobody looked at this one")
    expect(rail.insight_labels).to_have_count(1)


def test_the_line_that_two_words_cannot_carry_is_shown(diff, rail, staged):
    staged.put(_with_insights(("x", _label("bug", "high", about="only on cold start"))))
    diff.load("s1")
    expect(rail.insight_abouts).to_have_text(["only on cold start"])


def test_the_findings_can_be_narrowed_to_one_kind(diff, rail, staged):
    staged.put(_with_insights(
        ("an unbounded retry", _label("bug", "high")),
        ("a naming preference", _label("naming", "low")),
    ))
    diff.load("s1")
    expect(rail.pinned_insights).to_have_count(2)
    rail.narrow_to("naming")
    expect(rail.pinned_insights).to_have_count(1)
    expect(rail.pinned_insights.first).to_contain_text("a naming preference")


def test_one_kind_of_finding_offers_no_filter(diff, rail, staged):
    """A filter with a single option is a control that cannot do anything."""
    staged.put(_with_insights(("an unbounded retry", _label("bug", "high"))))
    diff.load("s1")
    expect(rail.theme_filter).to_have_count(0)


def test_the_reviewer_can_disagree_without_losing_the_finding(diff, rail, detail, staged):
    staged.put(_with_insights(("this name is confusing", _label("bug", "high"))))
    diff.load("s1")
    rail.pinned_insights.first.click()
    expect(detail.panel).to_be_visible()

    detail.relabel(theme="naming", criticality="low")
    expect(rail.insight_labels.first).to_contain_text("naming")
    expect(rail.pinned_insights.first).to_contain_text("this name is confusing")


def test_a_label_the_reviewer_set_says_so(diff, rail, detail, staged):
    """Their word replaces Claude's, and the rail shows which it is looking at."""
    staged.put(_with_insights(("x", _label("bug", "high"))))
    diff.load("s1")
    rail.pinned_insights.first.click()
    detail.relabel(criticality="low")
    expect(detail.label_note).to_have_text("your label")
    expect(rail.insight_labels.first).to_contain_text("✓")


def test_a_half_made_choice_survives_a_frame_arriving(diff, rail, detail, staged, as_agent, page):
    """A label is two choices sent as one command, so the second reads the first off the page. A
    frame landing in between used to rebuild these controls from the stored label and quietly put
    the first choice back — leaving a reviewer who picked both with a card that has neither."""
    from review_mate.session.commands import PostMessage

    staged.put(_with_insights(("nobody classified this", None)))
    diff.load("s1")
    rail.pinned_insights.first.click()
    expect(detail.panel).to_be_visible()

    detail.relabel(theme="security")                 # half a label: nothing is sent yet
    as_agent("s1", PostMessage(body="something else entirely"))
    expect(detail.label_theme).to_have_value("security")   # the frame must not have undone it

    detail.relabel(criticality="high")
    expect(rail.insight_labels.first).to_contain_text("security · high")
