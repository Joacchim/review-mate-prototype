"""The annotations: what the change owns, what its lines own, and the detail panel either opens."""
from __future__ import annotations

from playwright.sync_api import Page


class AnnotationsPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    @property
    def rows(self):
        """The highlight index — `#hlist`, so the MR row and the thread rows stay out of it."""
        return self.page.locator("#hlist .hrow")

    def row(self, n: int):
        """The row the reviewer knows as #N — the server's number, not a position."""
        return self.rows.filter(has=self.page.locator(".num", has_text=f"#{n}"))

    @property
    def insights(self):
        return self.page.locator("#ann .hrow:has(.chip.insight)")

    # --- the two zones -------------------------------------------------------

    @property
    def pin(self):
        """What the whole change owns, above the split — it does not scroll with the index."""
        return self.page.locator(".annpin")

    @property
    def review_pass(self):
        """The control that asks Claude for a pass over the whole change."""
        return self.page.locator(".passrow .btn")

    @property
    def pass_note(self):
        """Why the control is available again, when a pass has gone stale."""
        return self.page.locator(".passrow .passnote")

    @property
    def mr_row(self):
        return self.page.locator(".annpin .hrow.mr")

    @property
    def pinned_insights(self):
        return self.page.locator(".annpin .anninsights .hrow")

    # --- reading the findings by what matters --------------------------------

    def addressed(self, n: int):
        """The chip saying the agent changed the code over this row, rather than that it drifted."""
        return self.row(n).locator(".chip.fixed")

    def stale(self, n: int):
        return self.row(n).locator(".chip.stale")

    @property
    def insight_labels(self):
        """The theme·criticality chip on each insight, in the order the annotations lists them."""
        return self.page.locator(".annpin .anninsights .hrow .chip.crit")

    @property
    def insight_abouts(self):
        return self.page.locator(".annpin .anninsights .hrow .about")

    @property
    def theme_filter(self):
        """Offered only once there is more than one kind of finding to choose between."""
        return self.page.locator(".annpin .themes .chipbtn")

    def narrow_to(self, theme: str) -> None:
        self.page.locator(".annpin .themes .chipbtn", has_text=theme).first.click()

    @property
    def index_rows(self):
        """Every row in the scrolling zone, including the threads `rows` deliberately leaves out."""
        return self.page.locator(".annlist .hrow")

    def zone_sizes(self) -> dict:
        """What the split is for, in pixels: the cap, whether the insights scroll inside it, and
        whether the index kept any room at all."""
        return self.page.evaluate("""() => {
          const annotations = document.querySelector('.ann');
          const pin = document.querySelector('.annpin');
          const box = document.querySelector('.anninsights');
          const list = document.querySelector('.annlist');
          return {annotations: annotations.clientHeight, pin: pin.getBoundingClientRect().height,
                  insights_scroll: box.scrollHeight, insights_visible: box.clientHeight,
                  index: list.clientHeight};
        }""")

    def pin_is_outside_the_scroller(self) -> bool:
        return self.page.locator(".annlist .annpin").count() == 0

    # --- the detail panel ----------------------------------------------------

    @property
    def detail(self):
        return self.page.locator("#detail")

    @property
    def host_context(self):
        return self.page.locator("#detail .hostctx")

    def escalate(self, question: str = "") -> None:
        """Ask Claude for context on the highlight the detail panel is showing."""
        if question:
            self.detail.locator(".askinp").fill(question)
        self.detail.get_by_role("button", name="Ask Claude for context").click()

    @property
    def waiting(self):
        """The live "Claude is working" cue on an escalation with no card yet."""
        return self.page.locator("#detail .awtext")

    def dismiss_insight(self, index: int = 0) -> None:
        self.insights.nth(index).locator(".x").click()
