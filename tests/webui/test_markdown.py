"""What Claude writes, as the reviewer reads it.

The agent answers in structured prose — a heading, an argument in paragraphs, a numbered list of
findings — and the panel has to render that structure rather than flattening it. These are browser
tests because the structure is the point: a paragraph that renders as four disconnected lines is
not wrong in any string comparison, it is just unreadable.

Escaped first and parsed after, always. What an agent sends is text to be shown, never markup to
be run, and no amount of structure is worth loosening that.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import review_with_highlights
from webui.pages.detail import DetailPage
from webui.pages.diff import DiffPage
from webui.pages.annotations import AnnotationsPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def annotations(page) -> AnnotationsPage:
    return AnnotationsPage(page)


@pytest.fixture
def detail(page) -> DetailPage:
    return DetailPage(page)


def _said(body, session_id="s1"):
    from review_mate.session.state import ChatMessage
    state = review_with_highlights(session_id)
    state.messages = [ChatMessage(id="m1", role="assistant", body=body, anchor=None)]
    return state


def _render(diff, annotations, detail, staged, body):
    """Open the review's own conversation, where an MR-wide answer lands."""
    staged.put(_said(body))
    diff.load("s1")
    annotations.mr_row.click()
    expect(detail.messages).to_have_count(1)
    return detail.messages.first.locator(".md")


def test_wrapped_prose_is_one_paragraph(diff, annotations, detail, staged):
    """The case this exists for. An agent wraps its lines; rendering each as its own block turns
    an argument into a stack of unrelated assertions."""
    md = _render(diff, annotations, detail, staged,
                 "The helper reads as a backoff\nbut never sleeps between attempts,\n"
                 "so the retries fire together.")
    expect(md.locator("p")).to_have_count(1)
    assert "backoff but never sleeps between attempts, so the retries fire together" \
        in md.locator("p").inner_text()


def test_a_blank_line_starts_a_new_paragraph(diff, annotations, detail, staged):
    md = _render(diff, annotations, detail, staged, "What I found.\n\nWhat I would do about it.")
    expect(md.locator("p")).to_have_count(2)


def test_a_numbered_list_keeps_its_numbers(diff, annotations, detail, staged):
    """`1.` rendered as prose loses the ordering the agent meant, and as a bullet loses the
    numbers the reviewer refers back to."""
    md = _render(diff, annotations, detail, staged,
                 "1. the delay is accepted\n2. the delay is dropped\n3. the sleep never runs")
    expect(md.locator("ol")).to_have_count(1)
    expect(md.locator("ol > li")).to_have_count(3)


def test_a_nested_list_nests_inside_its_item(diff, annotations, detail, staged):
    """Not beside it: a sibling list reads as a second list at the same level, which says the
    sub-points are findings of their own."""
    md = _render(diff, annotations, detail, staged,
                 "- the retry helper\n  - takes a delay\n  - ignores it\n- the caller")
    expect(md.locator("ul > li > ul > li")).to_have_count(2)
    expect(md.locator("ul")).to_have_count(2)
    expect(md.locator("> ul > li")).to_have_count(2), "the outer list still has its own two items"


def test_prose_after_a_list_is_not_swallowed_by_it(diff, annotations, detail, staged):
    md = _render(diff, annotations, detail, staged,
                 "- one\n- two\n\nAnd what that means for the caller.")
    expect(md.locator("li")).to_have_count(2)
    # the placement is the assertion: a paragraph left inside the last item reads as a third
    # bullet that happens to be long, and the conclusion becomes one of the findings
    expect(md.locator("> p")).to_have_count(1)
    expect(md.locator("li p")).to_have_count(0)


def test_a_heading_orders_the_answer_without_claiming_the_panel(diff, annotations, detail, staged):
    """The agent's own sections sit below the headings this page uses for its own, or an answer
    starts reading like part of the tool."""
    md = _render(diff, annotations, detail, staged, "## What I found\n\nA missing await.")
    expect(md.locator(".mdh")).to_have_count(1)
    assert md.locator(".mdh").evaluate("el => el.tagName") in ("H4", "H5", "H6")


def test_a_fenced_block_keeps_its_lines_and_its_language(diff, annotations, detail, staged):
    md = _render(diff, annotations, detail, staged,
                 "Like this:\n\n```python\ndef drain():\n    retry()\n```")
    code = md.locator("pre.md code")
    expect(code).to_have_count(1)
    assert "def drain():\n    retry()" in code.inner_text()
    expect(code).to_have_class("lang-python")


def test_markup_an_agent_sends_is_shown_not_run(diff, annotations, detail, staged):
    """The rule the rest of this rests on. Structure is parsed out of text that was escaped
    first, so a tag in an answer is a tag the reviewer reads."""
    md = _render(diff, annotations, detail, staged,
                 "careful with <script>alert(1)</script> and <b>bold</b>")
    expect(md.locator("script")).to_have_count(0)
    expect(md.locator("b")).to_have_count(0)
    assert "<script>alert(1)</script>" in md.inner_text()
