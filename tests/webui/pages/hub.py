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
