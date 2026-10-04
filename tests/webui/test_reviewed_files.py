"""Working through a change file by file, and what a push does to that record.

The surface GitLab spells "viewed". What it is worth testing in a browser is not that a click
posts a command — the protocol suite covers that — but that the page says, without being asked,
which files are done and which of them the author has changed underneath you.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import review_with_a_file_read, two_file_review
from webui.pages.diff import DiffPage

CAPACITY = "capacity.py"
CONFIG = "config.py"


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


def test_nothing_is_read_until_the_reviewer_says_so(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.reviewed_files).to_have_count(0)
    expect(diff.progress).to_have_text("0 of 2 reviewed")
    expect(diff.reviewed_button).to_have_text("mark reviewed")


def test_marking_the_open_file_marks_it_in_the_tree_and_counts_it(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    diff.mark_reviewed()
    expect(diff.reviewed_button).to_have_text("✓ reviewed")
    expect(diff.tick(CAPACITY)).to_have_text("✓")
    expect(diff.progress).to_have_text("1 of 2 reviewed")
    expect(diff.reviewed_files).to_have_count(1)      # one file, not the change
    expect(diff.progress).not_to_contain_text("re-read")


def test_the_mark_comes_off_again(diff, staged):
    """Unmarking, from a file that arrived already read — rather than marking it here first.

    Two round-trips in one test made it a race, not a check: the second click had to land after
    the first had been applied, pushed, and re-rendered, and under load it sometimes did not.
    Marking is covered above; this is about taking a mark off.
    """
    staged.put(review_with_a_file_read("s1"))
    diff.load("s1")
    expect(diff.reviewed_button).to_have_text("✓ reviewed")
    expect(diff.progress).to_have_text("1 of 2 reviewed")
    diff.mark_reviewed()
    expect(diff.reviewed_button).to_have_text("mark reviewed")
    expect(diff.progress).to_have_text("0 of 2 reviewed")
    expect(diff.reviewed_files).to_have_count(0)


def test_a_file_read_before_the_page_opened_is_already_marked(diff, staged):
    """The mark is session state, so it survives a reload and arrives with the first view."""
    staged.put(review_with_a_file_read("s1"))
    diff.load("s1")
    expect(diff.tick(CAPACITY)).to_have_text("✓")
    expect(diff.progress).to_have_text("1 of 2 reviewed")


def test_a_file_the_author_changed_after_it_was_read_says_so(diff, staged):
    """Stale, not cleared: the reviewer decides they have re-read it, nothing decides for them.

    It stops counting, because the file is not read any more — and the line says how many are
    waiting, so a count that went down explains itself rather than looking like a bug.
    """
    staged.put(review_with_a_file_read("s1", stale=True))
    diff.load("s1")
    expect(diff.tick(CAPACITY)).to_have_text("✓")     # the same tick…
    expect(diff.stale_files).to_have_count(1)         # … greyed rather than green
    expect(diff.reviewed_button).to_have_text("✓! read again")
    expect(diff.progress).to_have_text("0 of 2 reviewed · 1 to re-read")


def test_reading_it_again_settles_a_stale_mark(diff, staged):
    staged.put(review_with_a_file_read("s1", stale=True))
    diff.load("s1")
    diff.mark_reviewed()
    expect(diff.reviewed_button).to_have_text("✓ reviewed")
    expect(diff.stale_files).to_have_count(0)
    expect(diff.progress).to_have_text("1 of 2 reviewed")


def test_the_other_file_is_untouched_by_any_of_it(diff, staged):
    staged.put(review_with_a_file_read("s1"))
    diff.load("s1")
    diff.open_file(CONFIG)
    expect(diff.reviewed_button).to_have_text("mark reviewed")
    expect(diff.tick(CONFIG)).to_have_count(0)
