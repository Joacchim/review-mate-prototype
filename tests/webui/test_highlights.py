"""The annotations in a browser: what the reviewer asked about, as the server folded it.

Every fact here reaches the page on `annotations:<sid>` — the numbering, the comment state, the card, the
host context. The page derives none of it, so a test that passes against a stale client-side
derivation would be the failure this suite exists to catch.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.commands import EmitCard

from webui.fixtures.scenarios import review_with_highlights, two_file_review
from webui.pages.diff import DiffPage
from webui.pages.annotations import AnnotationsPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def annotations(page) -> AnnotationsPage:
    return AnnotationsPage(page)


BLAME = [{"lines": [45, 46], "commit": "9f21ac0", "author": "mathieu", "date": "2025-11-02",
          "summary": "split the legacy queue out"}]
ISSUES = [{"iid": 412, "title": "retire the legacy queue", "url": "https://gitlab.example/i/412"}]


def test_the_annotations_list_what_the_reviewer_asked_about(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(annotations.rows).to_have_count(3)
    expect(annotations.rows.first).to_contain_text("scheduler/capacity.py:45-46")


def test_a_row_carries_the_number_the_session_gave_it(diff, annotations, staged):
    """#2 was removed before this session was staged: the annotations shows 1, 3, 4, never 1, 2, 3."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(annotations.rows.locator(".num")).to_have_text(["#1", "#3", "#4"])


def test_a_highlight_made_on_an_older_head_is_marked(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(annotations.row(4).locator(".chip.stale")).to_have_text("older ver")
    expect(annotations.row(1).locator(".chip.stale")).to_have_count(0)


def test_the_open_files_highlights_are_marked_in_the_diff(diff, staged):
    """Lines 45-46 and 47 of capacity.py — and nothing from the file that is not open."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(diff.highlighted_lines).to_have_count(3)
    diff.open_file("config.py")
    expect(diff.highlighted_lines).to_have_count(1)


def test_an_answered_highlight_shows_its_card(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.row(1).click()
    expect(annotations.detail).to_contain_text("#1")
    expect(annotations.detail.locator(".card")).to_contain_text("the pre-fleet queue")


def test_an_escalated_highlight_is_still_waiting(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.row(3).click()
    expect(annotations.detail.locator(".card")).to_have_count(0)
    expect(annotations.detail.locator(".awtext")).not_to_be_empty()


def test_an_mr_level_insight_stands_on_its_own(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(annotations.insights).to_have_count(1)
    expect(annotations.insights).to_contain_text("single queue")


def test_the_host_context_tier_lands_on_the_scope(diff, annotations, staged, stub_host):
    """The host read is not the page's to make: the scope reports loading, fetches, republishes."""
    stub_host.blame_lines = list(BLAME)
    stub_host.issues = list(ISSUES)
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.row(1).click()
    expect(annotations.host_context).to_contain_text("mathieu")
    expect(annotations.host_context).to_contain_text("split the legacy queue out")
    expect(annotations.host_context).to_contain_text("retire the legacy queue")


def test_a_host_with_no_last_touch_says_so(diff, annotations, staged, stub_host):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.row(1).click()
    expect(annotations.host_context).to_contain_text("no last-touch")


# --- interactions: a click leaves as a command and comes back as a new view ----

def test_asking_about_a_line_annotates_it(diff, annotations, staged):
    """No reload: the command lands, the session tail republishes, the annotations repaints."""
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(annotations.rows).to_have_count(0)
    diff.ask_about(45)
    expect(annotations.rows).to_have_count(1)
    expect(annotations.rows.first).to_contain_text("scheduler/capacity.py:45")
    expect(diff.highlighted_lines).to_have_count(1)


def test_dragging_asks_about_the_whole_range(diff, annotations, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    diff.ask_about(45, 46)
    expect(annotations.rows.first).to_contain_text("scheduler/capacity.py:45-46")
    expect(diff.highlighted_lines).to_have_count(2)


def test_asking_twice_about_a_line_discards_it(diff, annotations, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    diff.ask_about(45)
    expect(annotations.rows).to_have_count(1)
    diff.ask_about(45)
    expect(annotations.rows).to_have_count(0)


def test_escalating_marks_the_highlight_as_waiting(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.row(4).click()
    annotations.escalate("does anything still read this?")
    expect(annotations.detail.locator(".q")).to_have_text("does anything still read this?")
    expect(annotations.waiting).not_to_be_empty()
    # the index is the always-visible surface, so the escalation shows its own live cue there too
    expect(annotations.row(4).locator(".awtext")).not_to_be_empty()


def test_a_card_arriving_repaints_the_annotations(diff, annotations, staged, as_agent):
    """The agent's own path: a card the browser could not have sent lands on the open view."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.row(3).click()
    expect(annotations.detail.locator(".card")).to_have_count(0)
    as_agent("s1", EmitCard(highlight_id="h3", body="Nothing else reads `_legacy`."))
    expect(annotations.detail.locator(".card")).to_contain_text("Nothing else reads")
    expect(annotations.row(3)).to_contain_text("context ready")      # the row's preview, once answered


def test_dismissing_an_insight_removes_it(diff, annotations, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    expect(annotations.insights).to_have_count(1)
    annotations.dismiss_insight()
    expect(annotations.insights).to_have_count(0)


def test_a_selection_survives_a_frame_arriving_mid_drag(diff, annotations, staged, as_agent):
    """The diff is rebuilt whenever a scope it shows republishes, and a reviewer holding the mouse
    down has no say in when that happens. Losing the selection to it looks like the drag doing
    nothing — silently, and more often the more scopes a review watches."""
    from review_mate.session.commands import EmitCard

    staged.put(two_file_review("s1"))
    diff.load("s1")

    diff.press_line(45)
    as_agent("s1", EmitCard(highlight_id=None, body="something to say about the change"))
    expect(annotations.insights).to_have_count(1)        # the frame has landed and the diff re-rendered
    diff.release()

    expect(annotations.rows).to_have_count(1)
