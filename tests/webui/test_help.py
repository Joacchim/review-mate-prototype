"""Explaining the screen from inside it.

The `?` turns on a mode rather than a tour: the parts of the screen that can explain themselves
are outlined, and the reviewer points at whichever one they are wondering about. What is worth
testing here is not that a string appears — it is that every surface claiming to explain itself
still exists, and that nothing claims to explain a page it is not on.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.state import AccessRequest, AccessStatus
from webui.fixtures.scenarios import review_with_highlights
from webui.pages.annotations import AnnotationsPage
from webui.pages.detail import DetailPage
from webui.pages.diff import DiffPage
from webui.pages.hub import HubPage
from webui.pages.reviewbar import ReviewBarPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def hub(page, base_url) -> HubPage:
    return HubPage(page, base_url)


@pytest.fixture
def annotations(page) -> AnnotationsPage:
    return AnnotationsPage(page)


@pytest.fixture
def detail(page) -> DetailPage:
    return DetailPage(page)


@pytest.fixture
def review(page) -> ReviewBarPage:
    return ReviewBarPage(page)


def _registry(page):
    return page.evaluate("() => HELP.map(([sel, page, title]) => [sel, page, title])")


def _bubble(page):
    return page.locator("#helpbubble")


def test_the_screen_says_nothing_until_it_is_asked(diff, staged, page):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(_bubble(page)).to_be_hidden()
    expect(page.locator("body.helping")).to_have_count(0)
    page.locator("#files").hover()
    expect(_bubble(page)).to_be_hidden()      # hovering explains nothing while the mode is off


def test_asking_explains_whatever_is_pointed_at(diff, staged, page):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    page.locator("#t-help").click()
    page.locator("#files").hover()
    expect(_bubble(page)).to_contain_text("The files in the change")
    # the nearest thing to the pointer answers, not the panel holding it: a reviewer wondering
    # about the count at the top of the tree is not asking what a file tree is
    page.locator(".treeprog").hover()
    expect(_bubble(page)).to_contain_text("How far through the change you are")


def test_it_can_be_put_away_again(diff, staged, page):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    page.locator("#t-help").click()
    page.locator("#files").hover()
    expect(_bubble(page)).to_be_visible()
    page.keyboard.press("Escape")
    expect(_bubble(page)).to_be_hidden()
    expect(page.locator("body.helping")).to_have_count(0)


def test_a_control_says_the_same_thing_hovered_as_explained(diff, staged, page):
    """The tooltip is the short half of the entry, set from it — so the two cannot drift apart."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    page.locator("#t-help").click()
    page.locator("#t-commits").hover()
    expect(_bubble(page)).to_contain_text("The diff view mode: one commit at a time")
    assert page.locator("#t-commits").get_attribute("title") \
        == "The diff view mode: one commit at a time"


def test_the_landing_page_explains_itself_too(hub, staged, page):
    staged.put(review_with_highlights("s1"))
    hub.load()
    page.locator("#t-help").click()
    page.locator(".queuehdr").first.hover()
    expect(_bubble(page)).to_contain_text("Changes waiting on you")
    page.locator(".sitem").first.hover()
    expect(_bubble(page)).to_contain_text("One review you have open")


def test_the_diff_does_not_claim_the_page_it_is_not_showing(hub, staged, page):
    """`#diff` holds the landing page as well as a diff. Explaining it as a diff on the hub would
    describe something that is not open."""
    staged.put(review_with_highlights("s1"))
    hub.load()
    page.locator("#t-help").click()
    # the column holding the landing page, to the side of the page itself — the one place where
    # `#diff` is what the pointer is over and nothing nearer has anything to say
    def point_in(selector, dx, dy):
        box = page.locator(selector).first.bounding_box()
        page.mouse.move(box["x"] + dx, box["y"] + dy)       # a point, not an element's centre:
        page.wait_for_timeout(150)                          # the centre belongs to a child

    # below the landing page, which fills the column's width but not its height — the one point
    # where `#diff` is genuinely what the pointer is over
    land = page.locator(".land").bounding_box()
    column = page.locator("#diff").bounding_box()
    assert land["y"] + land["height"] + 40 < column["y"] + column["height"], "no space below it"
    page.mouse.move(column["x"] + 8, land["y"] + land["height"] + 40)
    page.wait_for_timeout(150)
    expect(_bubble(page)).not_to_contain_text("What you are reading")
    point_in(".land", 4, 200)          # the page itself, by its own margin
    expect(_bubble(page)).to_contain_text("Where a review starts")


def test_an_empty_surface_does_not_offer_to_explain_itself(hub, staged, page):
    """On the hub the file tree is an empty column. Outlining it would promise an answer about a
    change that is not open."""
    staged.put(review_with_highlights("s1"))
    hub.load()
    page.locator("#t-help").click()
    assert page.evaluate("() => getComputedStyle(document.getElementById('files')).outlineStyle") \
        == "none"


def test_every_surface_that_claims_to_explain_itself_still_exists(
        diff, hub, annotations, detail, review, staged, page):
    """The guard against the explanations going quietly stale. A renamed class or a dropped panel
    leaves an entry pointing at nothing, and nothing else would fail — the mode would simply have
    one fewer thing to say, which is exactly the failure nobody notices.

    The screens are driven far enough that every surface exists at some point: half of them appear
    only once a subject is open, and one channel at a time can be on screen. A guard that loaded
    each page and stopped would pass while saying nothing about those.
    """
    state = review_with_highlights("s1")
    state.access_requests = [AccessRequest(id="r1", repo="platform/shared",
                                           reason="it defines the type this calls",
                                           status=AccessStatus.PENDING)]
    staged.put(state)
    hub.load()
    entries = _registry(page)
    assert entries, "the registry is empty"

    seen = set()

    def look(where):
        page.wait_for_timeout(250)
        for selector, belongs, _title in entries:
            if (not belongs or belongs == where) and page.locator(selector).count():
                seen.add(selector)

    hub.load()
    look("hub")
    diff.load("s1")
    look("review")
    annotations.index_rows.first.click()          # the detail panel, on its Claude channel
    look("review")
    # every row, because a mark answered, a mark waiting and a mark nobody has asked about yet
    # put different things in the panel — and the ask itself only exists on the last of those
    for row in range(1, annotations.index_rows.count()):
        annotations.index_rows.nth(row).click()
        look("review")
    detail.tab("Review").click()                  # ... and on the comment being prepared
    expect(detail.draft_box).to_be_visible()
    look("review")
    detail.save_draft("something to send")        # ... which is what the review bar counts
    expect(review.counts).to_contain_text("pending")
    look("review")

    missing = [f"{sel} ({title}) — nothing matched it anywhere"
               for sel, _b, title in entries if sel not in seen]
    assert not missing, "\n".join(missing)
