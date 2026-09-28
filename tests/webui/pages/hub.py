"""The hub: open reviews and the review queue.

Selectors live here and nowhere else. Methods express intent, not clicks.
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


class ReviewRow:
    def __init__(self, page: Page, title: str) -> None:
        self.root = page.locator(".sitem").filter(has_text=title)

    @property
    def state_chip(self):
        return self.root.locator(".ststate")

    @property
    def meta(self):
        return self.root.locator(".m")

    def open(self) -> None:
        self.root.locator("a.rowlink").click()

    def close(self) -> None:
        self.root.locator("button.x").click()


class QueueRow:
    def __init__(self, page: Page, title: str) -> None:
        self.root = page.locator(".qitem").filter(has_text=title)

    def track(self) -> None:
        self.root.locator("button.track").click()

    def open(self) -> None:
        self.root.locator("a.rowlink").click()


class HubPage:
    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    def load(self) -> "HubPage":
        self.page.goto(self.base_url)
        expect(self.page.locator(".land")).to_be_visible()
        return self

    # --- open reviews -------------------------------------------------------

    @property
    def reviews(self):
        return self.page.locator(".sitem")

    def review(self, title: str) -> ReviewRow:
        return ReviewRow(self.page, title)

    def check_for_updates(self) -> None:
        self.page.get_by_role("button", name="check for updates").click()

    @property
    def checked_note(self):
        return self.page.locator(".hubago")

    # --- the queue ----------------------------------------------------------

    @property
    def queue(self):
        return self.page.locator(".qitem")

    def queue_entry(self, title: str) -> QueueRow:
        return QueueRow(self.page, title)

    def filter_queue(self, text: str) -> None:
        self.page.locator("input.qfilter").fill(text)

    @property
    def notices(self):
        return self.page.locator(".empty")

    @property
    def status(self):
        return self.page.locator("#status")


class SearchPage:
    """The landing page's search, and the way through it to Claude's lookup channel."""

    def __init__(self, page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    def look_for(self, text: str) -> None:
        self.page.goto(self.base_url)
        box = self.page.locator("#ref")
        box.fill(text)
        box.dispatch_event("input")

    @property
    def results(self):
        return self.page.locator(".land .searchresults .qitem")

    @property
    def error(self):
        return self.page.locator(".searcherr")

    @property
    def tracked(self):
        """Result rows that say they are already an open review."""
        return self.page.locator(".land .searchresults .qitem .chip.tracked")

    @property
    def track_buttons(self):
        return self.page.locator(".land .searchresults .qitem button.track")

    @property
    def ask_row(self):
        """Offered whatever the host search did — it can hit and still miss the right one."""
        return self.page.locator(".askrow")

    @property
    def ask_button(self):
        return self.page.locator(".askrow .btn")

    @property
    def described(self):
        """The description sent to Claude, which is not the term sent to the host."""
        return self.page.locator(".askrow .askbox")

    def describe(self, text: str) -> None:
        self.described.fill(text)

    def ask(self) -> None:
        self.ask_button.click()

    @property
    def answer(self):
        return self.page.locator(".claudepanel .claudeans")

    @property
    def candidates(self):
        return self.page.locator(".claudepanel .qitem")
