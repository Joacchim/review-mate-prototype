"""Working through a change file by file, and what a push does to that record.

The surface GitLab spells "viewed". What it is worth testing in a browser is not that a click
posts a command — the protocol suite covers that — but that the page says, without being asked,
which files are done and which of them the author has changed underneath you.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import markdown_review, review_with_a_file_read, two_file_review
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


# --- the file header's controls survive the panel being rebuilt under them ---------------------
#
# The diff panel is replaced rather than merged, deliberately and on measurement, and any topic
# frame replaces it — the presence tick alone produces one every few seconds. So a press and its
# release can fall either side of a rebuild, and a handler bound to the button that was pressed
# never hears about it. These drive exactly that: press, rebuild, release.

def _press_rebuild_release(page, locator):
    box = locator.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.evaluate("render()")          # the panel is torn down and rebuilt, as a frame would
    page.mouse.up()


def test_the_reviewed_toggle_survives_a_rebuild_between_press_and_release(diff, staged, page):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    _press_rebuild_release(page, diff.reviewed_button)
    expect(diff.progress).to_have_text("1 of 2 reviewed")


def test_the_markdown_toggle_survives_one_too(diff, staged, stub_host, page):
    stub_host.files["README.md"] = "# Title\n\nsome *emphasis* here\n"
    staged.put(markdown_review("s1"))
    diff.load("s1")
    expect(diff.markdown_view).to_have_count(0)
    _press_rebuild_release(page, page.get_by_role("button", name="rendered"))
    expect(diff.markdown_view).to_be_visible()


def test_the_kept_header_still_sits_below_what_a_mode_puts_above_it(diff, staged, stub_host,
                                                                    review_kb, page):
    """Hoisting the header out of the replaced region put it above the commit picker, where it
    had always been below. Only a screenshot caught that, which is too weak a net for an order
    the reader relies on."""
    from review_mate.session.state import ChangeType, FileEntry
    from webui.fixtures.scenarios import COMMIT_DIFF
    from webui.test_diff import stage_advanced_review
    stage_advanced_review(staged, stub_host, review_kb)
    stub_host.commit_files = {
        "aaaa111": [FileEntry(path="first.py", change_type=ChangeType.MODIFIED, language="python",
                              hunks=[{"diff": COMMIT_DIFF}])],
    }
    diff.load("s1")
    diff.toggle_per_commit()
    # both, before measuring either: a box is None while its element is still hidden, and the
    # header is hidden for as long as a mode that does not show a file is the one rendering
    expect(diff.commit_bar).to_be_visible()
    expect(page.locator(".fname")).to_be_visible()
    picker = diff.commit_bar.bounding_box()
    header = page.locator(".fname").bounding_box()
    assert picker["y"] < header["y"], (picker, header)   # the picker, then the file it is showing
