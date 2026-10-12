"""Regenerate the screenshots the feature documentation is built around.

    uv run --extra webtest python tools/screenshots.py

Drives the real application — the production `create_app` over staged sessions, the same
arrangement the browser tests use — so a picture cannot show a screen the product does not have.
Which is the point of scripting them: hand-taken screenshots go stale silently, and a reader
trusts a picture more than prose.

Add a shot by adding a `@shot` function. It gets a page on a staged session and saves one file.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests" / "webui"))
sys.path.insert(0, str(ROOT / "tests"))

from playwright.sync_api import sync_playwright  # noqa: E402

from _stage import (  # noqa: E402
    CAPACITY, CONFIG, FLEET, FOLDING_DIFF, HOST, IMAGES, QUEUE, VIEWPORT, WORKSPACE,
    branch_review, part_read, serving, showcase,
)
from review_mate.session.state import (  # noqa: E402
    ChangeType, DraftStatus, FileEntry, Highlight, LineRange, ReviewThread, Side, Theme,
)


# what is waiting on the reviewer, so the landing page shows a queue rather than an empty heading

SHOTS: list = []


def shot(name: str, description: str, shows: str, marks: tuple = (), height: int | None = None):
    """Register a shot.

    `shows` is a selector that must be visible when the picture is taken. Without it a mis-aimed
    shot is silent: the page renders *something*, the file is written, and the caption claims a
    screen the picture does not contain. That happened — a shot meant to show a consent prompt
    scrolled the wrong element and produced a second copy of the diff.

    `marks` are `(selector, label)` pairs, drawn as a ring around whatever the selector matches so
    the prose can point at a part of the screen rather than describing where to look. Anchored to
    the element and not to coordinates, so a ring cannot drift onto the wrong thing when the layout
    changes — it either lands on what it names or the run fails.
    """
    def register(fn):
        SHOTS.append((name, description, shows, tuple(marks), height, fn))
        return fn
    return register


# Drawn into the page rather than painted onto the file afterwards, so every ring is positioned
# from the element's own rectangle. Rings rather than arrows: an arrow needs a direction and some
# free space to come from, and picking those automatically goes wrong more often than it helps.
_MARK_JS = """
(marks) => {
  const layer = document.createElement('div');
  layer.id = '__marks';
  layer.style.cssText = 'position:fixed;inset:0;z-index:99999;pointer-events:none';
  document.body.appendChild(layer);
  const missing = [];
  const W = window.innerWidth, H = window.innerHeight;
  marks.forEach(([selector, label]) => {
    const el = document.querySelector(selector);
    if (!el) { missing.push(selector); return; }
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) { missing.push(selector); return; }
    const pad = 5;
    // clamped to the viewport: a ring that runs off the picture reads as a rendering fault, and a
    // badge outside it is simply not in the file
    const left = Math.max(2, r.left - pad), top = Math.max(2, r.top - pad);
    const right = Math.min(W - 2, r.right + pad), bottom = Math.min(H - 2, r.bottom + pad);
    const ring = document.createElement('div');
    ring.style.cssText =
      `position:fixed;left:${left}px;top:${top}px;` +
      `width:${right - left}px;height:${bottom - top}px;` +
      'border:3px solid #e5484d;border-radius:10px;' +
      'box-shadow:0 0 0 3px rgba(229,72,77,.22)';
    layer.appendChild(ring);
    if (!label) return;
    const badge = document.createElement('div');
    badge.textContent = label;
    badge.style.cssText =
      `position:fixed;left:${Math.max(2, left - 13)}px;top:${Math.max(2, top - 13)}px;` +
      'width:26px;height:26px;border-radius:50%;background:#e5484d;color:#fff;' +
      'font:600 15px/26px system-ui,sans-serif;text-align:center;' +
      'box-shadow:0 1px 4px rgba(0,0,0,.35)';
    layer.appendChild(badge);
  });
  return missing;
}
"""


# --- the change under review ---------------------------------------------------







# --- the shots -----------------------------------------------------------------



@shot("superseded", "reading a commit whose work a later one writes over",
      shows="tr.line.sup",
      marks=((".fname", "1"), ("tr.line.sup td.code", "2"),
             (".commitbar select", "3")))
def _superseded(page, base, stage):
    state = showcase()
    state.mr.capabilities = {**state.mr.capabilities, "commits": True}
    stage(state)
    HOST.commit_list = [
        {"sha": "9f3c1ab", "short_id": "9f3c1ab", "title": "reserve per fleet, falling back to legacy",
         "message": ""},
        {"sha": "abc123def", "short_id": "abc123d", "title": "bound the retry loop", "message": ""},
    ]
    HOST.commit_files = {"9f3c1ab": [
        FileEntry(path="scheduler/capacity.py", change_type=ChangeType.MODIFIED,
                  language="python", hunks=[{"diff": CAPACITY}])]}
    # the unbounded retry this commit wrote is what the next one fixes
    WORKSPACE.superseded_by_commit = {
        "9f3c1ab": {"scheduler/capacity.py": [{"start": 42, "end": 46, "sha": "abc123def"}]}}
    _open(page, base)
    page.locator("#t-commits").click()
    page.wait_for_selector("tr.line.sup")
    page.wait_for_timeout(500)


# A hunk far enough into the file that its leading gap survives being opened from both ends —
# below about forty lines the second step finishes the gap and there is no third control to show.


@shot("folding", "context opened around a hunk, and the band that puts it back",
      shows="tr.expand.folded",
      marks=(("tr.expand.folded td.ln", "1"), ("tr.expand.folded .exlink:nth-of-type(2)", "2"),
             ("tr.expand:not(.folded) td.ln", "3")))
def _folding(page, base, stage):
    # unfolding reads the file itself, so the host has to have one — a gap revealed against a host
    # with nothing behind it opens onto blank lines, which is a picture of a bug
    HOST.files["scheduler/capacity.py"] = "\n".join(
        [f"    def helper_{n}(self):" if n % 8 == 0 else f"        value_{n} = compute({n})"
         for n in range(1, 60)]
        + ["    def reserve(self, pu: ProcessingUnit) -> Reservation:",
           "        queue = self._queues.get(pu.fleet)", "        if queue is None:",
           "            queue = self._legacy", "        return queue.take(pu.size)"]
        + [f"        trailing_{n} = {n}" for n in range(1, 30)])
    state = showcase()
    state.files = [FileEntry(path="scheduler/capacity.py", change_type=ChangeType.MODIFIED,
                             language="python", hunks=[{"diff": FOLDING_DIFF}])]
    stage(state)
    _open(page, base)
    # opened from both ends, which is the state that carries every control at once
    for arrow in ("\u25bc", "\u25b2"):
        page.locator("tr.expand:not(.folded) .exlink").filter(has_text=arrow).first.click()
        page.wait_for_selector("tr.expand.folded")
        page.wait_for_timeout(500)


@shot("reviewed-files", "working through a change file by file",
      shows=".node.file.reviewed .rv",
      marks=((".treeprog", "1"), (".node.file.reviewed:not(.stale) .rv", "2"),
             (".node.file.stale .rv", "3"), (".fname .rvbtn", "4")))
def _reviewed_files(page, base, stage):
    stage(part_read())
    _open(page, base)


@shot("hub", "the landing page: what is open, and what is waiting on you", shows=".land",
      marks=(("#ref", "1"), (".land .hubhdr", "2"), (".land .queuehdr", "3")), height=620)
def _hub(page, base, stage):
    stage(showcase())
    page.goto(base)
    page.wait_for_selector(".land, #diff")
    page.wait_for_timeout(400)


@shot("diff", "the three panels a change is read in",
      shows="table.hunk .add",
      marks=(("#files", "1"), ("#diff", "2"), ("#ann", "3"),
             ("#t-left", "4"), ("#t-right", "5"), ("#mr", "6")))
def _diff(page, base, stage):
    stage(showcase())
    _open(page, base)


# a `gap` cell is produced only by the split renderer, so it proves the mode rather than the toggle
@shot("side-by-side", "the same hunk with both versions on one row", shows="table.hunk td.gap")
def _side_by_side(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.locator("#t-split").click()
    page.wait_for_timeout(300)


@shot("claude-channel", "what Claude found on a line, and the chat under it",
      shows="#detail .card",
      marks=(("#detail .tabs, #detail .chathdr", "1"), ("#detail .card", "2"),
             ("#detail .noteacts .btn", "3"), ("#detail .channelnote", "4")))
def _claude(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.locator("#hlist .hrow").first.click()
    page.wait_for_selector("#detail .msgs .msg")
    page.wait_for_timeout(300)


@shot("review-channel", "the comment you are preparing, which nobody else sees yet",
      shows="#detail textarea.draftbox",
      marks=(("#detail textarea.draftbox", "1"), ("#detail .channelnote", "2")))
def _review(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.locator("#hlist .hrow").first.click()
    page.wait_for_selector("#detail .tab")
    page.locator("#detail .tab", has_text="Review").click()
    page.wait_for_selector("#detail textarea.draftbox")
    page.wait_for_timeout(300)


@shot("review-state", "what is written, what is sent, and what the merge request will see",
      shows="#annseg .btn",
      marks=(("#annseg", "1"), (".annpin .hrow.mr", "2"), (".rbar, .reviewbar", "3")))
def _review_state(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.wait_for_selector("#annseg .btn")
    page.wait_for_timeout(300)


@shot("lookup", "finding a merge request by name, or by description when the name will not do",
      shows=".land .searchresults",
      marks=((".land .searchresults", "1"), (".land .askrow", "2")), height=620)
def _lookup(page, base, stage):
    stage(showcase())
    HOST.search_hits = [
        {"project": "platform/virtu/control-plane", "iid": 137,
         "title": "reserve scheduler capacity per fleet", "url": "https://gitlab.example/mr/137"},
        {"project": "platform/virtu/control-plane", "iid": 92,
         "title": "retire the single queue", "url": "https://gitlab.example/mr/92"},
    ]
    page.goto(base)
    page.wait_for_selector(".land")
    box = page.locator("#ref")
    box.fill("capacity")
    box.dispatch_event("input")
    page.wait_for_selector(".land .searchresults .qitem")
    page.wait_for_timeout(400)


@shot("per-commit", "reading the change one commit at a time",
      shows="table.hunk .add",      # the commit's own diff, not just the picker above it
      marks=(("#t-commits", "1"), ("#commitbar, .commitbar, .commits", "2")))
def _per_commit(page, base, stage):
    stage(showcase())
    HOST.commit_list = [
        {"sha": "9f3c1ab", "short_id": "9f3c1ab", "title": "reserve per fleet, falling back to legacy",
         "message": "", "author": "luigi", "created_at": "2026-02-01T08:00:00+00:00"},
        {"sha": "2b77e40", "short_id": "2b77e40", "title": "count what drain gave back",
         "message": "", "author": "luigi", "created_at": "2026-02-01T08:30:00+00:00"},
    ]
    # a commit with no diff illustrates nothing — the mode shows one commit's own change
    HOST.commit_files = {
        "9f3c1ab": [FileEntry(path="scheduler/capacity.py", change_type=ChangeType.MODIFIED,
                              language="python", hunks=[{"diff": CAPACITY}])],
        "2b77e40": [FileEntry(path="scheduler/config.py", change_type=ChangeType.MODIFIED,
                              language="python", hunks=[{"diff": CONFIG}])],
    }
    _open(page, base)
    page.locator("#t-commits").click()
    page.wait_for_timeout(600)


# the landing page rather than a diff: paired with the light one in the section below, it shows the
# same screen twice, which is the claim — one interface, two themes
@shot("dark", "the same landing page in the dark theme", shows=".land",
      marks=(("#t-theme", "1"),), height=620)
def _dark(page, base, stage):
    stage(showcase())
    page.goto(base)
    page.wait_for_selector(".land")
    page.locator("#t-theme").click()
    page.wait_for_timeout(400)


@shot("insights", "Claude's own read of the change, worst first",
      shows=".annpin .anninsights .chip.crit",
      marks=((".passrow .btn", "1"), (".annpin .anninsights .chip.crit", "2"),
             (".annpin .themes", "3")))
def _insights(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.wait_for_selector(".annpin .anninsights .hrow")
    page.wait_for_timeout(300)


@shot("consent", "Claude asking to read another repository, and nothing read until you answer",
      shows=".ann .req button",
      marks=((".ann .req .why", "1"), (".ann .req button", "2")))
def _consent(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.locator(".ann .req").first.scroll_into_view_if_needed()
    page.wait_for_timeout(300)


@shot("self-review", "reviewing your own branch: no merge request, and fixes landing as you comment",
      shows=".chip.fixed",
      marks=(("#mr", "1"), (".chip.fixed", "2")))
def _self_review(page, base, stage):
    stage(branch_review())
    _open(page, base)




def _open(page, base):
    page.goto(f"{base}/?s=s1")          # `s` is the parameter the page reads
    page.wait_for_selector("table.hunk tr")
    page.wait_for_timeout(500)


# --- the harness ---------------------------------------------------------------

def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    written = []
    with serving() as (base, stage), sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, description, shows, marks, height, take in SHOTS:
            # a short page in a tall frame is mostly empty, and empty is not worth 400 kB
            size = dict(VIEWPORT, height=height) if height else VIEWPORT
            page = browser.new_page(viewport=size, device_scale_factor=2)
            try:
                take(page, base, stage)
                if not page.locator(shows).first.is_visible():
                    raise SystemExit(
                        f"{name}: nothing matching {shows!r} is on screen, so the picture would "
                        f"not show what it claims — fix the shot rather than the caption")
                if marks:
                    missing = page.evaluate(_MARK_JS, [list(m) for m in marks])
                    if missing:
                        raise SystemExit(
                            f"{name}: nothing to mark for {missing} — the ring would point at "
                            f"nothing, so fix the selector rather than dropping the callout")
                target = IMAGES / f"{name}.png"
                page.screenshot(path=str(target))
                written.append((name, description, target.stat().st_size))
            finally:
                page.close()
        browser.close()

    for name, description, size in written:
        print(f"  docs/images/{name}.png  {size // 1024:>4} kB  — {description}")
    print(f"{len(written)} screenshots written to {IMAGES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
