"""The detail panel: one subject, and the two channels it is discussed in."""
from __future__ import annotations

from playwright.sync_api import Page


class DetailPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    @property
    def panel(self):
        return self.page.locator("#detail")

    # --- the two channels ----------------------------------------------------

    @property
    def tabs(self):
        return self.page.locator("#detail .tab")

    def tab(self, name: str):
        """`Claude` or `Review` — the channel, by the name the reviewer reads."""
        return self.page.locator("#detail .tab", has_text=name)

    @property
    def open_tab(self):
        return self.page.locator("#detail .tab.on")

    # --- what you can write, and who reads it --------------------------------

    @property
    def message_box(self):
        """The Claude channel's composer: a session command, seen only by the reviewer."""
        return self.page.locator("#detail .chatbox input")

    def ask(self, text: str) -> None:
        self.message_box.fill(text)
        self.message_box.press("Enter")

    @property
    def draft_box(self):
        """The review channel's composer: prose the whole merge request will read."""
        return self.page.locator("#detail textarea.draftbox")

    def save_draft(self, text: str) -> None:
        """Write the comment and commit it.

        The control is taken by position, not by label: it reads `Save` until a comment exists for
        this subject and `Update` afterwards, so editing one is the case a label match misses.
        """
        self.draft_box.fill(text)
        self.page.locator("#detail .draftbtns .btn").first.click()

    @property
    def messages(self):
        """The Claude channel's chat as the server folded it — so its arrival is the proof
        a message made the round trip, not a delay."""
        return self.page.locator("#detail .msgs .msg")

    # --- doubting what was said ----------------------------------------------

    def doubt(self, index: int = 0) -> None:
        """Ask Claude to verify the claim in one message of the chat."""
        self.messages.nth(index).locator(".noteacts .btn", has_text="double-check").click()

    def doubt_card(self) -> None:
        """Ask Claude to verify its own answer — the claim above the chat."""
        self.panel.locator(".noteacts .btn", has_text="double-check this").click()

    @property
    def checking(self):
        return self.panel.locator(".checkwait")

    # --- disagreeing with how Claude classified it ---------------------------

    @property
    def label_theme(self):
        return self.panel.locator('.labelrow select[aria-label="theme"]')

    @property
    def label_criticality(self):
        return self.panel.locator('.labelrow select[aria-label="criticality"]')

    @property
    def label_note(self):
        """Shown once the label is the reviewer's rather than Claude's."""
        return self.panel.locator(".labelrow .labelnote")

    def relabel(self, theme: str | None = None, criticality: str | None = None) -> None:
        if theme is not None:
            self.label_theme.select_option(theme)
        if criticality is not None:
            self.label_criticality.select_option(criticality)

    def close(self) -> None:
        self.page.locator("#detail .dclose").click()

    # --- reading it over the whole window ------------------------------------

    @property
    def full_view(self):
        return self.page.locator("#detail .dmax", has_text="Full view")

    @property
    def reading_width(self):
        return self.page.locator("#detail .dmax", has_text="Reading width")

    @property
    def full_width(self):
        return self.page.locator("#detail .dmax", has_text="Full width")

    @property
    def maximised(self):
        return self.page.locator("#detail.max")

    def widths(self) -> dict:
        """The panel against the window, and the text column against the panel — a mode whose only
        evidence is a class name proves nothing."""
        return self.page.evaluate("""() => {
          const d = document.getElementById('detail');
          const b = d.querySelector('.dbody');
          // `cap` is the measure in force, if any. The reading width is set in `ch`, so what it
          // comes to in pixels is whatever the browser's font makes it — asking the page rather
          // than assuming a number keeps a test about the behaviour from becoming one about fonts.
          const cap = parseFloat(getComputedStyle(b).maxWidth);
          return {panel: Math.round(d.getBoundingClientRect().width),
                  body: Math.round(b.getBoundingClientRect().width),
                  cap: Number.isFinite(cap) ? Math.round(cap) : null,
                  window: window.innerWidth};
        }""")
