"""The review bar: what is prepared to send, and the controls that send it."""
from __future__ import annotations

from playwright.sync_api import Page


class ReviewBarPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    @property
    def bar(self):
        return self.page.locator(".reviewbar")

    @property
    def counts(self):
        """`Your review · N pending · M posted` — the server's numbers, not the page's."""
        return self.bar.locator("span").first

    @property
    def approve(self):
        return self.bar.locator(".approve-tog input")

    @property
    def you_approved(self):
        return self.bar.locator(".chip.you-approved")

    @property
    def submit(self):
        return self.bar.locator("button.primary")

    # --- the version banner, which shares the bar's facts --------------------

    @property
    def banner(self):
        return self.page.locator(".verbanner")

    def mark_reviewed(self) -> None:
        self.banner.locator("button", has_text="Mark reviewed").first.click()
