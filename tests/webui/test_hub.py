"""The hub surface in a browser.

What is asserted here needs a browser: that a view renders, that an interaction sends the right
command, and that a pushed update repaints. What a topic contains is the protocol suite's job.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import mr, review_with_drafts, session, two_file_review
from webui.pages.hub import HubPage


@pytest.fixture
def hub(page, base_url) -> HubPage:
    return HubPage(page, base_url)


def test_an_empty_hub_shows_the_queue(hub):
    hub.load()
    expect(hub.reviews).to_have_count(0)
    expect(hub.queue).to_have_count(2)
    expect(hub.queue_entry("reserve Scheduler capacity").root).to_be_visible()


def test_a_staged_review_is_listed(hub, staged):
    staged.put(two_file_review("s1"))
    hub.load()
    expect(hub.review("reserve Scheduler capacity").root).to_be_visible()
    expect(hub.review("reserve Scheduler capacity").meta).to_contain_text("control-plane !137")


def test_an_mr_already_open_is_not_offered_in_the_queue_as_well(hub, staged):
    staged.put(two_file_review("s1"))
    hub.load()
    expect(hub.reviews).to_have_count(1)
    expect(hub.queue).to_have_count(1)          # the other queue entry only
    expect(hub.queue_entry("allow transfer with no user").root).to_be_visible()


def test_tracking_moves_an_mr_into_open_reviews(hub, staged):
    hub.load()
    expect(hub.queue).to_have_count(2)
    hub.queue_entry("reserve Scheduler capacity").track()
    # the command lands, the hub republishes, and the page repaints without a reload
    expect(hub.reviews).to_have_count(1)
    assert [ref.iid for ref in staged.created] == [137]


def test_closing_a_review_removes_it(hub, staged):
    staged.put(two_file_review("s1"))
    hub.load()
    expect(hub.reviews).to_have_count(1)
    hub.review("reserve Scheduler capacity").close()
    expect(hub.reviews).to_have_count(0)
    assert staged.ended == ["s1"]


def test_a_review_with_unsubmitted_drafts_says_so(hub, staged):
    staged.put(review_with_drafts("s1"))
    hub.load()
    expect(hub.review("reserve Scheduler capacity").meta).to_contain_text("1 draft")


def test_host_state_is_not_claimed_before_a_check(hub, staged):
    """The verdict chip reports host-derived state, so it stays absent until one has run."""
    staged.put(two_file_review("s1"))
    hub.load()
    expect(hub.review("reserve Scheduler capacity").state_chip).to_have_count(0)
    expect(hub.checked_note).to_have_count(0)


def test_checking_for_updates_prices_in_the_host(hub, staged):
    staged.put(two_file_review("s1"))
    hub.load()
    hub.check_for_updates()
    expect(hub.review("reserve Scheduler capacity").state_chip).to_be_visible()
    expect(hub.checked_note).to_contain_text("checked")


def test_a_failing_queue_read_is_shown_not_swallowed(hub, staged, stub_host):
    stub_host.fail_with = RuntimeError("gitlab 503")
    hub.load()
    expect(hub.notices.filter(has_text="unavailable")).to_be_visible()
    expect(hub.reviews).to_have_count(0)        # the local half still rendered


def test_the_queue_filters(hub):
    hub.load()
    expect(hub.queue).to_have_count(2)
    hub.filter_queue("dr-house")
    expect(hub.queue).to_have_count(1)


def test_a_review_is_a_real_link(hub, staged):
    """Every entry is a destination, so it can be middle-clicked into its own tab."""
    staged.put(two_file_review("s1"))
    hub.load()
    link = hub.review("reserve Scheduler capacity").root.locator("a.rowlink")
    expect(link).to_have_attribute("href", "?s=s1")
