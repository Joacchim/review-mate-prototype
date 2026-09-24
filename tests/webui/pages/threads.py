"""The discussions already on the merge request: the list, and one of them open."""
from __future__ import annotations

from playwright.sync_api import Page


class ThreadsPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    # --- the list -----------------------------------------------------------

    @property
    def heading(self):
        return self.page.locator(".rail h3", has_text="Discussions")

    @property
    def rows(self):
        """Discussion rows, which the rail renders after its per-line index."""
        return self.page.locator(".raillist .hrow:has(.chip)").filter(
            has=self.page.locator(".chip.comment, .chip.posted")).filter(
            has_not=self.page.locator(".num:text-matches('^#')"))

    def row(self, text: str):
        return self.page.locator(".raillist .hrow", has_text=text)

    def show(self, which: str) -> None:
        """`Unresolved` or `All` — the filter a reviewer arrives with, and the other one.

        Named apart from the per-line index's own filter, which reads alike and sits in the same
        scroller.
        """
        self.page.locator(".threadseg .btn", has_text=which).click()

    def refresh(self) -> None:
        self.page.locator(".raillist .btn", has_text="refresh").click()

    def jump_from(self, text: str) -> None:
        """Click a discussion's location, which takes the diff to the line it is about."""
        self.row(text).locator(".loc").click()

    # --- one of them, open in the panel --------------------------------------

    @property
    def comments(self):
        return self.page.locator("#detail .msgs .msg")

    @property
    def reply_box(self):
        return self.page.locator("#detail textarea.draftbox")

    def reply(self, text: str) -> None:
        self.reply_box.fill(text)
        self.page.locator("#detail .draftbtns .btn", has_text="Reply").first.click()

    def resolve(self) -> None:
        self.page.locator("#detail .draftbtns .btn", has_text="Resolve").first.click()

    def comment(self, text: str):
        return self.page.locator("#detail .msgs .msg", has_text=text)

    def actions_on(self, text: str):
        """Edit and delete, offered only on the reviewer's own comment."""
        return self.comment(text).locator(".noteacts .btn")
