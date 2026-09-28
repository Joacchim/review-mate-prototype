"""Cross-repo consent: what Claude has asked to read, and answering it."""
from __future__ import annotations

from playwright.sync_api import Page


class ConsentPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    @property
    def requests(self):
        """One block per repository asked about, answered or not."""
        return self.page.locator(".ann .req")

    @property
    def waiting(self):
        """The ones still owed an answer — the only ones that offer the two buttons."""
        return self.page.locator(".ann .req:not(.decided)")

    def outcome(self, repo: str):
        """What the answer produced: refused, being fetched, ready at a path, or failed."""
        return self.request(repo).locator(".grant")

    def request(self, repo: str):
        return self.requests.filter(has=self.page.locator(".repo", has_text=repo))

    def reason(self, repo: str):
        return self.request(repo).locator(".why")

    def allow(self, repo: str) -> None:
        self.request(repo).locator("button", has_text="Approve").click()

    def refuse(self, repo: str) -> None:
        self.request(repo).locator("button", has_text="Deny").click()
