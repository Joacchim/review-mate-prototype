"""A control the reviewer can see.

An active toggle used to paint itself with the accent colour as its background while keeping the
accent colour as its text, so `⇄ split`, `⑃ commits` and every filter in the panel became a blank
blue rectangle the moment you used them. Nothing failed: the label was in the DOM, the button
worked, and only a screenshot showed it.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import review_with_highlights
from webui.pages.diff import DiffPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


def _unreadable(page):
    """Every button whose text is the colour of what it sits on."""
    return page.evaluate("""() => {
      const out = [];
      document.querySelectorAll('button').forEach(el => {
        const s = getComputedStyle(el);
        if (!el.textContent.trim() || !el.offsetParent) return;
        if (s.color === s.backgroundColor) out.push(el.textContent.trim());
      });
      return out;
    }""")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_an_active_control_can_still_be_read(diff, page, staged, theme):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    page.wait_for_selector("table.hunk tr")
    if theme == "dark":
        page.locator("#t-theme").click()
        page.wait_for_timeout(200)

    page.locator("#t-split").click()          # a view toggle
    page.locator("#t-commits").click()        # and another
    page.wait_for_timeout(300)
    assert _unreadable(page) == []


def test_a_filter_can_still_be_read_once_it_is_the_one_chosen(diff, page, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    page.wait_for_selector("#railseg .btn")
    page.locator("#railseg .btn", has_text="Comments").click()
    page.wait_for_timeout(200)
    assert _unreadable(page) == []
