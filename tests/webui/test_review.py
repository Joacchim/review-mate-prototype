"""Preparing a review in a browser, and sending it.

Every number here is the server's: what is pending, what has been posted, whether this reviewer has
approved. The page used to work them out from session state and two fetches of its own, which is
why these assert the counts and the approval rather than only that a button exists — a page still
deriving them would pass a test that just clicked things.

The thread half of this surface (list, filter, jump, reply, resolve) belongs to S6 and is not here.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.state import DraftComment, DraftStatus

from webui.fixtures.scenarios import review_with_highlights, two_file_review
from webui.pages.detail import DetailPage
from webui.pages.diff import DiffPage
from webui.pages.rail import RailPage
from webui.pages.reviewbar import ReviewBarPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def rail(page) -> RailPage:
    return RailPage(page)


@pytest.fixture
def detail(page) -> DetailPage:
    return DetailPage(page)


@pytest.fixture
def review(page) -> ReviewBarPage:
    return ReviewBarPage(page)


def _approvable(state):
    """An MR whose host says approvals are supported.

    The bar asks the server, and the server requires an explicit yes — a host that reports no
    capabilities cannot be approved through, so offering the control would offer a failure.
    """
    state.mr.capabilities = dict(state.mr.capabilities or {}, approvals=True)
    return state


def _with_draft(body="this needs a test", highlight_id=None, status=DraftStatus.DRAFT):
    state = _approvable(review_with_highlights("s1"))
    state.drafts = [DraftComment(id="d1", highlight_id=highlight_id, body=body, status=status)]
    return state


# --- what is waiting to be sent ---------------------------------------------

def test_a_review_with_nothing_prepared_offers_the_approval_only(diff, review, staged):
    staged.put(_approvable(two_file_review("s1")))
    diff.load("s1")
    expect(review.counts).to_contain_text("0 pending")
    expect(review.submit).to_be_disabled()


def test_the_counts_are_the_servers(diff, review, staged):
    staged.put(_with_draft())
    diff.load("s1")
    expect(review.counts).to_contain_text("1 pending")
    expect(review.submit).to_be_enabled()


def test_a_posted_comment_moves_between_the_counts(diff, review, staged):
    staged.put(_with_draft(status=DraftStatus.POSTED))
    diff.load("s1")
    expect(review.counts).to_contain_text("0 pending")
    expect(review.counts).to_contain_text("1 posted")


# --- writing one ------------------------------------------------------------

def test_drafting_at_mr_level_reaches_the_bar(diff, rail, detail, review, staged):
    """Written in the panel, counted by the server, shown in the bar — with no page arithmetic."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rail.mr_row.click()
    detail.tab("Review").click()
    detail.save_draft("reads well overall")
    expect(review.counts).to_contain_text("1 pending")


def test_drafting_on_a_highlight_reaches_the_bar(diff, rail, detail, review, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rail.index_rows.first.click()
    detail.tab("Review").click()
    detail.save_draft("this needs a test")
    expect(review.counts).to_contain_text("1 pending")


def test_editing_a_draft_does_not_make_a_second_one(diff, rail, detail, review, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rail.index_rows.first.click()
    detail.tab("Review").click()
    detail.save_draft("half a thought")
    expect(review.counts).to_contain_text("1 pending")
    detail.save_draft("the whole thought")
    expect(review.counts).to_contain_text("1 pending")


# --- sending it -------------------------------------------------------------

def test_submitting_posts_every_prepared_comment(diff, review, staged, stub_writer):
    staged.put(_with_draft("reads well overall"))
    diff.load("s1")
    review.submit.click()
    expect(review.counts).to_contain_text("1 posted")
    assert stub_writer.posted == ["reads well overall"]
    assert stub_writer.approved is False


def test_submitting_reports_what_landed(diff, review, staged):
    staged.put(_with_draft())
    diff.load("s1")
    review.submit.click()
    expect(review.status).to_contain_text("posted 1 comment")


def test_approving_travels_with_the_submission(diff, review, staged, stub_writer):
    staged.put(_with_draft("reads well overall"))
    diff.load("s1")
    review.approve.check()
    review.submit.click()
    expect(review.status).to_contain_text("approved")
    assert stub_writer.approved is True
    assert stub_writer.posted == ["reads well overall"]


def test_an_mr_the_host_cannot_approve_offers_no_approval(diff, review, staged):
    """The server requires an explicit capability, so a control that would fail is not offered."""
    staged.put(review_with_highlights("s1"))       # no capabilities reported
    diff.load("s1")
    expect(review.approve).to_have_count(0)
