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
        self.draft_box.fill(text)
        self.page.locator("#detail .draftbtns .btn", has_text="Save").first.click()

    @property
    def messages(self):
        """The Claude channel's conversation as the server folded it — so its arrival is the proof
        a message made the round trip, not a delay."""
        return self.page.locator("#detail .msgs .msg")

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
          return {panel: Math.round(d.getBoundingClientRect().width),
                  body: Math.round(b.getBoundingClientRect().width),
                  window: window.innerWidth};
        }""")
