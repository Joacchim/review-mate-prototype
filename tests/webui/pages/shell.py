"""The chrome around every page: the header, and the light that says what Claude is doing."""
from __future__ import annotations

from playwright.sync_api import Page


class ShellPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    @property
    def agent(self):
        """The header light. Its class carries the state the server joined, not one derived here."""
        return self.page.locator("#agent")

    @property
    def status(self):
        """What the last action reported — the header line every command writes its outcome to."""
        return self.page.locator("#status")

    @property
    def agent_label(self):
        """The word beside it — shown only when the state needs the reviewer's eye."""
        return self.page.locator("#agent .alab")
