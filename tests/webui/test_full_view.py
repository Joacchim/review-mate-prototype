"""Reading one chat over the whole window.

Full view is a mode, not a route: the same panel, the same subject, the same channel, given the
frame. What the tests pin is that nothing is lost on the way in or out — the reviewer keeps the tab
they were reading and the chat stays subscribed — and that the width actually changes,
since a mode whose only evidence is a class name proves nothing.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.commands import RemoveHighlight
from review_mate.session.state import Origin

from webui.fixtures.scenarios import review_with_highlights
from webui.pages.detail import DetailPage
from webui.pages.diff import DiffPage
from webui.pages.annotations import AnnotationsPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def annotations_page(page) -> AnnotationsPage:
    return AnnotationsPage(page)


@pytest.fixture
def opened(page, diff, staged) -> DetailPage:
    """A review with its first highlight open in the panel."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    AnnotationsPage(page).index_rows.first.click()
    detail = DetailPage(page)
    expect(detail.panel).to_be_visible()
    return detail


def test_full_view_gives_the_panel_the_window(opened):
    before = opened.widths()
    opened.full_view.click()
    after = opened.widths()
    assert after["panel"] > before["panel"] * 1.5, (before, after)
    assert after["panel"] > after["window"] * 0.9, after
    # and it opens edge to edge rather than on a measure
    assert after["body"] > after["panel"] * 0.9, after


def test_reading_width_puts_the_text_back_on_a_measure(opened):
    opened.full_view.click()
    wide = opened.widths()
    opened.reading_width.click()
    narrow = opened.widths()
    assert narrow["panel"] == wide["panel"], (wide, narrow)   # the panel is unchanged
    assert narrow["body"] < wide["body"], (wide, narrow)      # only the column narrows

    # Not a ratio against the wide width. The measure is set in `ch`, so what it comes to in pixels
    # is whatever font the machine happens to have: 961 here, 1069 on one carrying only the basic
    # fonts — enough to put a 0.85 ratio on the wrong side and fail CI while the behaviour was
    # right. What holds anywhere is that a cap is in force, that it is narrower than the panel
    # rather than the panel itself, and that the column is sitting on it.
    assert narrow["cap"] is not None, narrow
    assert narrow["cap"] < narrow["panel"], narrow
    assert abs(narrow["body"] - narrow["cap"]) <= 1, narrow


def test_the_width_toggle_goes_both_ways(opened):
    """Narrowing is only half of it — the control has to give the frame back."""
    opened.full_view.click()
    wide = opened.widths()
    opened.reading_width.click()
    expect(opened.full_width).to_have_count(1)      # the label flips with the state
    opened.full_width.click()
    expect(opened.reading_width).to_have_count(1)
    assert opened.widths()["body"] == wide["body"]


def test_escape_leaves_the_mode_and_keeps_the_subject(opened):
    opened.full_view.click()
    expect(opened.maximised).to_have_count(1)
    opened.page.keyboard.press("Escape")
    expect(opened.maximised).to_have_count(0)
    expect(opened.panel).to_be_visible()          # the subject is still open


def test_the_channel_survives_the_mode(opened):
    """Switching to Review, then growing the panel, must not drop the reviewer back on Claude."""
    opened.tab("Review").click()
    opened.full_view.click()
    expect(opened.open_tab).to_contain_text("Review")


def test_the_conversation_stays_subscribed_across_the_mode(opened):
    # the subscription is answered a frame later than the click, so wait for it to land first
    opened.page.wait_for_function("() => Object.keys(topicViews).some(s => /^chat:s1:/.test(s))")
    before = opened.page.evaluate("Object.keys(topicViews).filter(s => /^chat:s1:/.test(s))")
    opened.full_view.click()
    expect(opened.maximised).to_have_count(1)   # the mode has settled before the topics are read
    after = opened.page.evaluate("Object.keys(topicViews).filter(s => /^chat:s1:/.test(s))")
    assert before == after and len(after) == 1, (before, after)


def test_losing_the_subject_leaves_full_view_behind(page, opened, annotations_page, as_agent, staged):
    """Full view is a mode the panel is in, so it must end when the panel does — by whichever route.

    The reviewer cannot reach the annotations to discard anything while the panel covers the window, but a
    removal from elsewhere still arrives: another client, or the agent dismissing what it raised.
    The panel closes on that, and if the mode outlived it the next subject would open maximised.
    """
    opened.full_view.click()
    expect(opened.maximised).to_have_count(1)

    as_agent("s1", RemoveHighlight(highlight_id="h1"), origin=Origin.BROWSER)
    expect(opened.panel).to_be_hidden()

    annotations_page.index_rows.first.click()
    expect(opened.panel).to_be_visible()
    expect(opened.maximised).to_have_count(0)
