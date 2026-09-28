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
from webui.pages.annotations import AnnotationsPage
from webui.pages.reviewbar import ReviewBarPage
from webui.pages.shell import ShellPage
from webui.pages.threads import ThreadsPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def annotations(page) -> AnnotationsPage:
    return AnnotationsPage(page)


@pytest.fixture
def detail(page) -> DetailPage:
    return DetailPage(page)


@pytest.fixture
def review(page) -> ReviewBarPage:
    return ReviewBarPage(page)


@pytest.fixture
def threads(page) -> ThreadsPage:
    return ThreadsPage(page)


@pytest.fixture
def shell(page) -> ShellPage:
    return ShellPage(page)


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

def test_drafting_at_mr_level_reaches_the_bar(diff, annotations, detail, review, staged):
    """Written in the panel, counted by the server, shown in the bar — with no page arithmetic."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.mr_row.click()
    detail.tab("Review").click()
    detail.save_draft("reads well overall")
    expect(review.counts).to_contain_text("1 pending")


def test_drafting_on_a_highlight_reaches_the_bar(diff, annotations, detail, review, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.index_rows.first.click()
    detail.tab("Review").click()
    detail.save_draft("this needs a test")
    expect(review.counts).to_contain_text("1 pending")


def test_editing_a_draft_does_not_make_a_second_one(diff, annotations, detail, review, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.index_rows.first.click()
    detail.tab("Review").click()
    detail.save_draft("half a thought")
    expect(review.counts).to_contain_text("1 pending")
    detail.save_draft("the whole thought")
    expect(review.counts).to_contain_text("1 pending")


# --- sending it -------------------------------------------------------------

def test_submitting_posts_every_prepared_comment(diff, review, staged, stub_host_writer):
    staged.put(_with_draft("reads well overall"))
    diff.load("s1")
    review.submit.click()
    expect(review.counts).to_contain_text("1 posted")
    assert stub_host_writer.posted == ["reads well overall"]
    assert stub_host_writer.approved is False


def test_submitting_reports_what_landed(diff, review, shell, staged):
    staged.put(_with_draft())
    diff.load("s1")
    review.submit.click()
    expect(shell.status).to_contain_text("posted 1 comment")


def test_approving_travels_with_the_submission(diff, review, shell, staged, stub_host_writer):
    staged.put(_with_draft("reads well overall"))
    diff.load("s1")
    review.approve.check()
    review.submit.click()
    expect(shell.status).to_contain_text("approved")
    assert stub_host_writer.approved is True
    assert stub_host_writer.posted == ["reads well overall"]


def test_an_mr_the_host_cannot_approve_offers_no_approval(diff, review, staged):
    """The server requires an explicit capability, so a control that would fail is not offered."""
    staged.put(review_with_highlights("s1"))       # no capabilities reported
    diff.load("s1")
    expect(review.approve).to_have_count(0)


# --- the discussions already on the merge request ----------------------------
# The last of this surface. Every fact here is the server's: which comments are the reviewer's own,
# how many are still open, what a re-sync left behind. The page compared usernames for the first of
# those and fetched its own identity to do it.

def _discussed(*rows, session_id="s1"):
    from review_mate.session.state import ReviewThread, ThreadComment
    state = review_with_highlights(session_id)
    state.threads = [
        ReviewThread(id=r["id"], resolved=r.get("resolved", False), anchor=r.get("anchor"),
                     comments=[ThreadComment(id=str(i), author=a, body=b)
                               for i, (a, b) in enumerate(r.get("said", []))])
        for r in rows]
    return state


AT_LINE = {"file": "scheduler/capacity.py", "side": "new", "line": 45}


def test_the_discussions_are_listed_with_what_was_said(diff, threads, staged):
    staged.put(_discussed({"id": "d1", "anchor": AT_LINE,
                           "said": [("eric", "prefer a guard here")]}))
    diff.load("s1")
    expect(threads.heading).to_be_visible()
    expect(threads.row("prefer a guard here")).to_be_visible()


def test_the_filter_starts_on_what_is_still_open(diff, threads, staged):
    staged.put(_discussed({"id": "d1", "said": [("eric", "still open")]},
                          {"id": "d2", "resolved": True, "said": [("eric", "settled already")]}))
    diff.load("s1")
    expect(threads.row("still open")).to_be_visible()
    expect(threads.row("settled already")).to_have_count(0)
    threads.show("All")
    expect(threads.row("settled already")).to_be_visible()


def test_a_discussions_location_takes_the_diff_to_it(diff, threads, page, staged):
    staged.put(_discussed({"id": "d1", "anchor": AT_LINE, "said": [("eric", "prefer a guard")]}))
    diff.load("s1")
    threads.jump_from("prefer a guard")
    expect(page.locator("table.hunk tr.flash")).to_have_count(1)


def test_only_the_reviewers_own_comments_offer_edit_and_delete(diff, threads, staged):
    """The server says whose a comment is. The page used to fetch its own identity and compare."""
    staged.put(_discussed({"id": "d1", "anchor": AT_LINE,
                           "said": [("someone-else", "not yours"), ("reviewer", "yours")]}))
    diff.load("s1")
    threads.row("not yours").click()
    expect(threads.comments).to_have_count(2)
    expect(threads.actions_on("yours")).to_have_count(2)        # edit + delete
    expect(threads.actions_on("not yours")).to_have_count(0)    # and none on someone else's


def test_replying_reaches_the_host(diff, threads, staged, stub_host_writer):
    staged.put(_discussed({"id": "d1", "anchor": AT_LINE, "said": [("eric", "prefer a guard")]}))
    diff.load("s1")
    threads.row("prefer a guard").click()
    threads.reply("fixed in the next push")
    expect(threads.reply_box).to_have_value("")        # the box clears once it has gone
    assert stub_host_writer.replied == [("d1", "fixed in the next push")]


def test_resolving_a_discussion_settles_it(diff, threads, shell, staged, stub_host_writer):
    staged.put(_discussed({"id": "d1", "anchor": AT_LINE, "said": [("eric", "prefer a guard")]}))
    diff.load("s1")
    threads.row("prefer a guard").click()
    threads.resolve()
    # the report is what says the round trip finished; what the list then shows is whatever the
    # host answers with, which this fixture decides rather than the resolve does
    expect(shell.status).to_have_text("resolved")
    assert stub_host_writer.resolved == [("d1", True)]


# --- asking for a pass over the whole change ----------------------------------
# The reviewer asks before or alongside their own pass, so it blocks nothing. What the control
# must never do is disappear, or stop saying anything after the branch moves.

def _reviewed_at(sha, requested=False):
    state = review_with_highlights("s1")
    state.mr = state.mr.model_copy(update={"sha": sha})
    if requested:
        state.insights_requested = True
        state.insights_requested_at = "2026-01-01T00:00:00+00:00"
        state.insights_requested_sha = sha
    return state


def test_a_change_nobody_has_asked_about_offers_the_pass(diff, annotations, staged):
    staged.put(_reviewed_at("abc123"))
    diff.load("s1")
    expect(annotations.review_pass).to_be_enabled()


def test_a_pass_covering_this_code_greys_the_control_rather_than_hiding_it(diff, annotations, staged):
    """A control that vanishes reads as broken; one that is greyed reads as already done."""
    staged.put(_reviewed_at("abc123", requested=True))
    diff.load("s1")
    expect(annotations.review_pass).to_have_count(1)
    expect(annotations.review_pass).to_be_disabled()


def test_a_pass_the_change_moved_past_says_so_and_offers_another(diff, annotations, staged):
    state = _reviewed_at("abc123", requested=True)
    state.mr = state.mr.model_copy(update={"sha": "moved-on"})   # a push since the pass
    staged.put(state)
    diff.load("s1")
    expect(annotations.pass_note).to_contain_text("about an earlier version")
    expect(annotations.review_pass).to_be_enabled()


def test_asking_records_the_request(diff, annotations, staged):
    staged.put(_reviewed_at("abc123"))
    diff.load("s1")
    annotations.review_pass.click()
    expect(annotations.review_pass).to_be_disabled()      # the server answered, and the control followed
