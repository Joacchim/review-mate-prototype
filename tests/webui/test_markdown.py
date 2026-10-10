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


# --- a place in the code, written the way everyone writes one --------------------------------

def test_a_file_and_line_becomes_a_link(diff, annotations, detail, staged):
    md = _render(diff, annotations, detail, staged,
                 "The caller is in scheduler/capacity.py:45 and never checks the result.")
    ref = md.locator("a.coderef")
    expect(ref).to_have_count(1)
    assert ref.get_attribute("data-path") == "scheduler/capacity.py"
    assert ref.get_attribute("data-line") == "45"


def test_a_reference_in_backticks_links_too(diff, annotations, detail, staged):
    """Where Claude usually writes one, so excluding code spans would miss most of them."""
    md = _render(diff, annotations, detail, staged, "Look at `scheduler/capacity.py:45`.")
    expect(md.locator("code a.coderef")).to_have_count(1)


def test_a_bare_filename_is_left_as_prose(diff, annotations, detail, staged):
    """Linking every mention of a file turns an answer into a field of links, and a file named in
    passing is not somewhere the reviewer was told to go."""
    md = _render(diff, annotations, detail, staged, "The helper lives in capacity.py these days.")
    expect(md.locator("a.coderef")).to_have_count(0)


def test_prose_that_merely_looks_like_one_is_left_alone(diff, annotations, detail, staged):
    """A wrong link is worse than no link: it is indistinguishable from a right one until it is
    followed, and then it has moved the reviewer somewhere for no reason."""
    md = _render(diff, annotations, detail, staged,
                 "It regressed by 3.14 and the e.g. case is rare.")
    expect(md.locator("a.coderef")).to_have_count(0)


def test_an_external_link_is_not_rewritten_inside_itself(diff, annotations, detail, staged):
    """The author's own link wins — a link inside a link is not a thing."""
    md = _render(diff, annotations, detail, staged,
                 "see [the pipeline](https://gitlab.com/x/y/-/jobs/1) for app.py:12")
    expect(md.locator("a[href]")).to_have_count(1)
    expect(md.locator("a.coderef")).to_have_count(1)


def test_following_a_reference_opens_that_file_at_that_line(diff, annotations, detail, staged,
                                                            page):
    """The point of the whole thing: it goes where it points."""
    md = _render(diff, annotations, detail, staged,
                 "The guard is missing at scheduler/capacity.py:45.")
    md.locator("a.coderef").click()
    expect(page.locator("#diff .fname")).to_contain_text("scheduler/capacity.py")
    expect(page.locator('#diff td.code[data-line="45"]')).to_be_visible()


def test_a_reference_outside_the_change_opens_the_browser_and_goes_there(
        diff, annotations, detail, staged, stub_host, page):
    """The half that is not just a jump. A file the change does not touch is read through the
    repository browser, which is a subscription rather than a mode — so following a reference
    into one has to turn it on, wait for the read, and land on the line."""
    stub_host.repo_tree = ["scheduler/capacity.py", "docs/retries.md"]
    stub_host.files["docs/retries.md"] = "\n".join(f"line {n}" for n in range(1, 30))
    staged.put(_said("The rule is written down in docs/retries.md:12."))
    diff.load("s1")
    assert page.evaluate("Object.keys(topicViews).filter(s => /^tree:/.test(s))") == []

    annotations.mr_row.click()
    detail.messages.first.locator("a.coderef").click()

    page.wait_for_function("() => Object.keys(topicViews).some(s => /^tree:/.test(s))")
    # the plain-file header, which says in so many words that this is not part of the change
    header = page.locator("#diff .fname", has_text="related file")
    expect(header).to_have_count(1)
    expect(header).to_contain_text("docs/retries.md")
    expect(page.locator('#diff td.code[data-line="12"]')).to_be_visible()
