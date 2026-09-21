"""The rail: the highlight index, and the detail panel it opens."""
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
