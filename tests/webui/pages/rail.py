"""The rail: what the change owns, what its lines own, and the detail panel either opens."""
from __future__ import annotations

from playwright.sync_api import Page


class RailPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    @property
    def rows(self):
        """The highlight index — `#hlist`, so the MR row and the thread rows stay out of it."""
        return self.page.locator("#hlist .hrow")

    def row(self, n: int):
        """The row the reviewer knows as #N — the server's number, not a position."""
        return self.rows.filter(has=self.page.locator(".num", has_text=f"#{n}"))

    @property
    def insights(self):
        return self.page.locator("#rail .hrow:has(.chip.insight)")

    # --- the two zones -------------------------------------------------------

    @property
    def pin(self):
        """What the whole change owns, above the split — it does not scroll with the index."""
        return self.page.locator(".railpin")

    @property
    def mr_row(self):
        return self.page.locator(".railpin .hrow.mr")

    @property
    def pinned_insights(self):
        return self.page.locator(".railpin .railinsights .hrow")

    @property
    def index_rows(self):
        """Every row in the scrolling zone, including the threads `rows` deliberately leaves out."""
        return self.page.locator(".raillist .hrow")

    def zone_sizes(self) -> dict:
        """What the split is for, in pixels: the cap, whether the insights scroll inside it, and
        whether the index kept any room at all."""
        return self.page.evaluate("""() => {
          const rail = document.querySelector('.rail');
          const pin = document.querySelector('.railpin');
          const box = document.querySelector('.railinsights');
          const list = document.querySelector('.raillist');
          return {rail: rail.clientHeight, pin: pin.getBoundingClientRect().height,
                  insights_scroll: box.scrollHeight, insights_visible: box.clientHeight,
                  index: list.clientHeight};
        }""")

    def pin_is_outside_the_scroller(self) -> bool:
        return self.page.locator(".raillist .railpin").count() == 0

    # --- the detail panel ----------------------------------------------------

    @property
    def detail(self):
        return self.page.locator("#detail")

    @property
    def cheap_context(self):
        return self.page.locator("#detail .cheapctx")

    def escalate(self, question: str = "") -> None:
        """Ask Claude for context on the highlight the detail panel is showing."""
        if question:
            self.detail.locator(".askinp").fill(question)
        self.detail.get_by_role("button", name="Ask Claude for context").click()

    @property
    def waiting(self):
        """The live "Claude is working" cue on an escalation with no card yet."""
        return self.page.locator("#detail .awtext")

    def dismiss_insight(self, index: int = 0) -> None:
        self.insights.nth(index).locator(".x").click()
