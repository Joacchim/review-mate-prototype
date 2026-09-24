"""Cross-repo consent: what Claude has asked to read, and answering it."""
from __future__ import annotations

from playwright.sync_api import Page


class ConsentPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    @property
    def requests(self):
        """One block per repository still waiting on an answer."""
        return self.page.locator(".rail .req")

    def request(self, repo: str):
        return self.requests.filter(has=self.page.locator(".repo", has_text=repo))

    def reason(self, repo: str):
        return self.request(repo).locator(".why")

    def allow(self, repo: str) -> None:
        self.request(repo).locator("button", has_text="Approve").click()

    def refuse(self, repo: str) -> None:
        self.request(repo).locator("button", has_text="Deny").click()
