"""The review surface: the file tree and the rendered diff."""
from __future__ import annotations

from playwright.sync_api import Page, expect


class DiffPage:
    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    def load(self, session_id: str) -> "DiffPage":
        self.page.goto(f"{self.base_url}/?s={session_id}")
        expect(self.table).to_be_visible()
        return self

    # --- the file tree ------------------------------------------------------

    @property
    def files(self):
        return self.page.locator(".node.file")

    def open_file(self, name: str) -> None:
        self.files.filter(has_text=name).click()

    # --- the diff -----------------------------------------------------------

    @property
    def table(self):
        return self.page.locator("table.hunk")

    @property
    def rows(self):
        return self.page.locator("table.hunk tr.line")

    def rows_of(self, kind: str):
        """kind is add, del or ctx."""
        return self.page.locator(f"table.hunk tr.line.{kind}")

    @property
    def selectable_lines(self):
        return self.page.locator("table.hunk td.code[data-line]")

    def line(self, number: int):
        return self.page.locator(f'table.hunk td.code[data-line="{number}"]')

    def token(self, kind: str):
        """kind is a CSS token class suffix: kw, str, cmt, num…"""
        return self.page.locator(f"table.hunk .tok-{kind}")

    # --- unfolding ----------------------------------------------------------

    @property
    def unfold_bands(self):
        return self.page.locator("table.hunk tr.expand")

    def unfold_all(self) -> None:
        self.page.locator("table.hunk tr.expand .exlink").filter(has_text="all").first.click()

    # --- toggles ------------------------------------------------------------

    def toggle_side_by_side(self) -> None:
        self.page.locator("#t-split").click()

    def toggle_markdown(self) -> None:
        self.page.get_by_role("button", name="rendered").click()

    @property
    def markdown_view(self):
        return self.page.locator(".mdview")

    # --- diff modes ---------------------------------------------------------

    @property
    def version_banner(self):
        return self.page.locator(".verbanner")

    def show_since_last(self) -> None:
        self.version_banner.get_by_role("button", name="Since last review").click()

    def show_full_diff(self) -> None:
        self.version_banner.get_by_role("button", name="Full diff").click()

    def toggle_per_commit(self) -> None:
        self.page.locator("#t-commits").click()

    @property
    def commit_bar(self):
        return self.page.locator(".commitbar")

    @property
    def notice(self):
        return self.page.locator("#diff .empty")
