"""The rail's two zones in a browser: what stays within reach, and what scrolls under it.

The MR-level comment and Claude's own insights are pinned, because those are what a reviewer wants
at hand whatever they are reading; the per-line index scrolls beneath them. The point of the split
is that the pin cannot push the index out of reach, which is what the cap here is for — a review
that collects a dozen insights must not bury its own index.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.state import Card

from webui.fixtures.scenarios import review_with_highlights
from webui.pages.diff import DiffPage
from webui.pages.rail import RailPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def rail(page) -> RailPage:
    return RailPage(page)


# the scenario already carries one MR-level card (`c2`), so the zone shows one more than is added
SCENARIO_INSIGHTS = 1


def _with_insights(added: int, session_id="s1"):
    state = review_with_highlights(session_id)
    state.cards = list(state.cards) + [
        Card(id=f"i{n}", highlight_id=None,
             body=f"MR-level insight {n}: the reducer is the only writer of this field")
        for n in range(added)
    ]
    return state


def test_what_the_change_owns_is_pinned_and_what_a_line_owns_scrolls(diff, rail, staged):
    staged.put(_with_insights(2))
    diff.load("s1")
    expect(rail.pin).to_have_count(1)
    # the MR-level comment row and every insight are in the zone
    expect(rail.mr_row).to_have_count(1)
    expect(rail.pinned_insights).to_have_count(2 + SCENARIO_INSIGHTS)
    # and the index below carries the per-line rows instead
    assert rail.index_rows.count() >= 3      # the session's three highlights


def test_the_pinned_zone_is_not_inside_the_scroller(rail, diff, staged):
    """If it were, scrolling the index would carry it away — which is the thing being fixed."""
    staged.put(_with_insights(1))
    diff.load("s1")
    assert rail.pin.count() == 1                 # it exists at all
    assert rail.pin_is_outside_the_scroller()    # and is not nested in the scroller


def test_insights_cannot_grow_the_zone_past_its_cap(diff, rail, staged):
    """Twelve insights scroll inside the zone rather than pushing the index off the screen."""
    staged.put(_with_insights(12))
    diff.load("s1")
    sizes = rail.zone_sizes()
    assert sizes["pin"] <= sizes["rail"] * 0.47, sizes                      # capped
    assert sizes["insights_scroll"] > sizes["insights_visible"], sizes      # so they scroll within it
    assert sizes["index"] > 0, sizes                                        # and the index keeps room


def test_the_index_is_reachable_without_scrolling_past_the_insights(diff, rail, staged):
    """The first anchored row is on screen with the rail unscrolled, however many insights there are."""
    staged.put(_with_insights(12))
    diff.load("s1")
    expect(rail.index_rows.first).to_be_in_viewport()
