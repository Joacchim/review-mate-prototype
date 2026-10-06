"""The review surface: the file tree, the rendered diff, and the highlight overlay on it."""
from __future__ import annotations

import re

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

    # --- files the reviewer has finished reading ----------------------------

    @property
    def reviewed_files(self):
        return self.page.locator(".node.file.reviewed")

    @property
    def stale_files(self):
        """Read, and changed by the author since — ticked, but greyed and uncounted."""
        return self.page.locator(".node.file.reviewed.stale")

    def tick(self, name: str):
        """The tree's mark on one file: ✓ read, ✓! read and changed since."""
        return self.files.filter(has_text=name).locator(".rv")

    @property
    def progress(self):
        return self.page.locator(".treeprog")

    @property
    def reviewed_button(self):
        """The toggle in the open file's header, whatever state it is in."""
        return self.page.locator(".fname .rvbtn")

    def mark_reviewed(self) -> None:
        self.reviewed_button.click()

    # --- the diff -----------------------------------------------------------

    @property
    def table(self):
        return self.page.locator("table.hunk")

    @property
    def file_header(self):
        return self.page.locator(".fname")

    @property
    def superseded_rows(self):
        """Lines of the commit being read that the branch does not end up with."""
        return self.page.locator("table.hunk tr.line.sup")

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
        # not every band offers an unfold: a gap that has been opened carries one that puts it back
        return self.page.locator("table.hunk tr.expand:not(.folded)")

    @property
    def fold_bands(self):
        return self.page.locator("table.hunk tr.expand.folded")

    # A band that still hides more than one step says "⋯ all N"; one that hides less says
    # "⋯ show N lines", and one for a file whose length is not known yet says "show rest of file".
    # All three open the gap completely, and which appears is a property of the gap, not the test.
    _OPEN_IT_ALL = re.compile(r"all|show")

    def unfold_all(self) -> None:
        # scoped away from the fold band, whose "fold all" would otherwise match first
        self.unfold_bands.locator(".exlink").filter(has_text=self._OPEN_IT_ALL).first.click()

    def unfold(self, arrow: str) -> None:
        """One step of the unfold band: ▼ reveals downward from the top, ▲ upward from the end."""
        self.unfold_bands.locator(".exlink").filter(has_text=arrow).first.click()

    def fold(self, text: str = "fold") -> None:
        """The fold band: ⤴ steps the top run back, ⤵ the bottom. The whole-gap control reads
        "fold all N" while part of the gap is still shut and "fold N lines back" once it is open
        all the way, so the default matches the word both of them share."""
        self.fold_bands.locator(".exlink").filter(has_text=text).first.click()

    def gaps_held(self) -> dict:
        """What the page is still holding unfolded, by file — the state the bands are built from."""
        return self.page.evaluate(
            "() => Object.fromEntries(Object.entries(expandedGaps)"
            ".map(([p, m]) => [p, Object.fromEntries(m)]))")

    # --- asking about lines --------------------------------------------------

    def ask_about(self, first: int, last: int | None = None) -> None:
        """Click a line, or drag from one to another — what commits a highlight."""
        self.press_line(first)
        if last is not None and last != first:
            self.line(last).hover()
        self.release()

    def press_line(self, number: int) -> None:
        """Begin a selection and leave it open, so a test can interleave something with it."""
        self.line(number).hover()
        self.page.mouse.down()

    def release(self) -> None:
        self.page.mouse.up()

    # --- the highlight overlay ----------------------------------------------

    @property
    def highlighted_lines(self):
        return self.page.locator("table.hunk tr.line.hl")

    # --- toggles ------------------------------------------------------------

    def show_all_repo_files(self) -> None:
        """The file browser: every path in the repository, not only the changed ones."""
        self.page.locator(".treehdr input[type=checkbox]").click()

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
