"""A subject the agent changed the code over, rather than one whose lines merely drifted.

The two look identical in the session — a head that no longer matches where the highlight was made
— and mean opposite things. Reviewing a branch before it leaves the machine makes the second case
the ordinary one, so a client that read staleness alone would fill the annotations with warnings about the
reviewer's own progress.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.state import Addressed, Subject, SubjectKind

from webui.fixtures.scenarios import review_with_highlights
from webui.pages.diff import DiffPage
from webui.pages.annotations import AnnotationsPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def annotations(page) -> AnnotationsPage:
    return AnnotationsPage(page)


def _fixed(state, highlight_id, sha="def4567", summary="bounded the retry at five"):
    state.addressed = [Addressed(subject=Subject(kind=SubjectKind.HIGHLIGHT, id=highlight_id),
                                 sha=sha, summary=summary, at="2026-01-01T00:05:00+00:00")]
    return state


def test_a_highlight_the_agent_fixed_reads_as_addressed(diff, annotations, staged):
    # h4 was made on an older sha, so without a record it reads as merely drifted
    staged.put(_fixed(review_with_highlights("s1"), "h4"))
    diff.load("s1")
    expect(annotations.addressed(4)).to_have_count(1)
    # the warning is replaced, not stacked beside it
    expect(annotations.stale(4)).to_have_count(0)


def test_a_highlight_that_only_drifted_still_warns(diff, annotations, staged):
    """The original reading, still right when nobody caused the head to move."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(annotations.stale(4)).to_have_count(1)
    expect(annotations.addressed(4)).to_have_count(0)


def test_what_was_changed_is_readable_without_opening_it(diff, annotations, staged):
    state = _fixed(review_with_highlights("s1"), "h4")
    staged.put(state)
    diff.load("s1")
    expect(annotations.addressed(4)).to_have_attribute("title", "bounded the retry at five (def4567)")


def test_an_untouched_highlight_says_nothing_either_way(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(annotations.addressed(1)).to_have_count(0)
    expect(annotations.stale(1)).to_have_count(0)


# --- a branch is not a merge request ------------------------------------------

def _branch(state):
    from review_mate.session.state import MRMetadata
    state.mr = MRMetadata(
        host="local", project="control-plane", iid=0, title="reserve scheduler capacity",
        source_branch="feat/fleet-capacity", target_branch="main", sha="9f3c1ab", author="you",
        url="/home/you/src/control-plane", clone_url="/home/you/src/control-plane",
        capabilities={"threads": False, "approvals": False})
    state.threads = []
    return state


def test_a_branch_is_named_by_where_it_is_going_not_by_a_number(diff, page, staged):
    """`!0` is what a client invents when it assumes every review is a merge request."""
    staged.put(_branch(review_with_highlights("s1")))
    diff.load("s1")
    header = page.locator("#mr")
    expect(header).to_contain_text("feat/fleet-capacity → main")
    expect(header).not_to_contain_text("!0")


def test_a_branch_is_not_called_the_merge_request(diff, page, staged):
    staged.put(_branch(review_with_highlights("s1")))
    diff.load("s1")
    expect(page.locator(".annpin h3")).to_contain_text("The branch")
    expect(page.locator(".ann")).to_contain_text("nobody else is reading this yet")


def test_a_merge_request_still_links_back_to_the_host(diff, page, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(page.locator("#mr .mrlink")).to_contain_text("!137")
