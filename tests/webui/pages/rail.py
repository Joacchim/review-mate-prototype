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
