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
sys.path.insert(0, str(ROOT / "tests" / "webui"))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from review_mate.server.app import create_app  # noqa: E402
from review_mate.seams import MRRef  # noqa: E402
from review_mate.session.state import (  # noqa: E402
    AccessRequest, Addressed, Card, ChangeType, ChatMessage, Criticality, DraftComment,
    DraftStatus, FileEntry, Highlight, Label, LineRange, MRMetadata, ReviewThread, ThreadComment,
    SessionState, SessionStatus, Side, Subject, SubjectKind, Theme,
)
from webui.fixtures.host import StubHost  # noqa: E402
from webui.fixtures.manager import FakeManager  # noqa: E402

IMAGES = ROOT / "docs" / "images"
HOST = StubHost()          # shots that need host-computed context set it here

# what is waiting on the reviewer, so the landing page shows a queue rather than an empty heading
QUEUE = [
    {"host": "gitlab", "project": "platform/virtu/control-plane", "iid": 141,
     "title": "drop the per-fleet lock from the fast path",
     "url": "https://gitlab.example/mr/141"},
    {"host": "gitlab", "project": "platform/orchestration/dr-house", "iid": 86,
     "title": "allow a transfer with no user attached", "url": "https://gitlab.example/mr/86"},
    {"host": "gitlab", "project": "platform/virtu/fleet-api", "iid": 12,
     "title": "expose capacity per fleet", "url": "https://gitlab.example/mr/12"},
]
VIEWPORT = {"width": 1500, "height": 900}

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

CAPACITY = """@@ -38,12 +38,18 @@ class Scheduler:
     def reserve(self, pu: ProcessingUnit) -> Reservation:
-        queue = self._queues[pu.fleet]
-        return queue.take(pu.size)
+        queue = self._queues.get(pu.fleet)
+        if queue is None:
+            queue = self._legacy
+        while True:
+            try:
+                return queue.take(pu.size)
+            except Contended:
+                continue
 
     def release(self, reservation: Reservation) -> None:
         reservation.queue.give_back(reservation.size)
@@ -71,9 +77,13 @@ class Scheduler:
     def drain(self, fleet: str) -> int:
         # give every reservation on a fleet back, and say how many there were
-        held = self._held.pop(fleet, [])
-        for reservation in held:
-            self.release(reservation)
-        return len(held)
+        held = self._held.pop(fleet, [])
+        drained = 0
+        for reservation in held:
+            self.release(reservation)
+            drained += 1
+        if drained:
+            log.info("drained %d reservations from %s", drained, fleet)
+        return drained
"""

CONFIG = """@@ -10,7 +10,7 @@
 # how long a reservation may be held before the scheduler takes it back
-RESERVATION_TIMEOUT = 30
+RESERVATION_TIMEOUT = 120
 
 # the fleet a processing unit falls back to when its own is unknown
 LEGACY_FLEET = "legacy"
"""

FLEET = """@@ -0,0 +1,9 @@
+from dataclasses import dataclass
+
+
+@dataclass(frozen=True)
+class Fleet:
+    name: str
+    capacity: int
+
+    def can_take(self, size: int) -> bool:
+        return size <= self.capacity
"""


def showcase(session_id: str = "s1") -> SessionState:
    """A review far enough along to show what the tool is for: lines marked, context answered,
    a comment being written, and Claude's own reading of the change beside the reviewer's."""
    anchor = Subject(kind=SubjectKind.HIGHLIGHT, id="h1")
    return SessionState(
        id=session_id, status=SessionStatus.ACTIVE, created_at="2026-02-01T09:00:00+00:00", seq=42,
        mr=MRMetadata(
            host="gitlab", project="platform/virtu/control-plane", iid=137,
            title="reserve scheduler capacity per fleet", source_branch="feat/fleet-capacity",
            target_branch="main", sha="abc123def", author="luigi",
            url="https://gitlab.example/mr/137",
            capabilities={"threads": True, "approvals": True, "commits": True,
                          "diff_versions": True, "inline_comments": True},
            diff_refs={"base_sha": "0ldbase", "head_sha": "abc123def"}),
        files=[
            FileEntry(path="scheduler/capacity.py", change_type=ChangeType.MODIFIED,
                      language="python", hunks=[{"diff": CAPACITY}]),
            FileEntry(path="scheduler/config.py", change_type=ChangeType.MODIFIED,
                      language="python", hunks=[{"diff": CONFIG}]),
            FileEntry(path="scheduler/fleet.py", change_type=ChangeType.ADDED,
                      language="python", hunks=[{"diff": FLEET}]),
        ],
        highlights=[
            Highlight(id="h1", ordinal=1, file="scheduler/capacity.py", side=Side.NEW,
                      line_range=LineRange(start=45, end=48),
                      question="why retry forever instead of bounding it?",
                      created_at="2026-02-01T09:12:00+00:00", created_sha="abc123def",
                      context_requested=True, context_requested_at="2026-02-01T09:12:00+00:00"),
            Highlight(id="h2", ordinal=2, file="scheduler/config.py", side=Side.NEW,
                      line_range=LineRange(start=12, end=12),
                      created_at="2026-02-01T09:20:00+00:00", created_sha="abc123def"),
        ],
        cards=[
            Card(id="c1", highlight_id="h1", created_at="2026-02-01T09:13:00+00:00",
                 body=("`Contended` is raised when another scheduler holds the fleet's lock. The "
                       "previous code let it propagate and the caller retried with backoff — "
                       "`retry_reserve` in `api/reserve.py:88`, which this bypasses.\n\n"
                       "Nothing bounds this loop, so a fleet that stays contended spins.")),
            Card(id="c2", highlight_id=None, created_at="2026-02-01T09:30:00+00:00",
                 label=Label(theme=Theme.BUG, criticality=Criticality.HIGH,
                             about="the retry path", by="agent"),
                 body=("The reservation loop has no bound and no sleep. Under contention this is a "
                       "busy-wait holding the GIL.")),
            Card(id="c3", highlight_id=None, created_at="2026-02-01T09:31:00+00:00",
                 label=Label(theme=Theme.TEST, criticality=Criticality.MEDIUM,
                             about="no coverage for the legacy fallback", by="agent"),
                 body="`_legacy` is reached only when a fleet is unknown, and no test constructs that."),
            Card(id="c4", highlight_id=None, created_at="2026-02-01T09:32:00+00:00",
                 label=Label(theme=Theme.NAMING, criticality=Criticality.LOW, by="agent"),
                 body="`can_take` reads as a question but is used as a guard; `fits` would be plainer."),
        ],
        messages=[
            ChatMessage(id="m1", role="user", anchor=anchor,
                        body="is anything else relying on Contended propagating?",
                        created_at="2026-02-01T09:15:00+00:00"),
            ChatMessage(id="m2", role="agent", anchor=anchor,
                        body=("Two call sites. `api/reserve.py:88` catches it and backs off, and "
                              "`tests/test_contention.py` asserts it reaches the caller — that test "
                              "will fail with this change."),
                        created_at="2026-02-01T09:16:00+00:00"),
        ],
        drafts=[
            DraftComment(id="d1", highlight_id="h1", status=DraftStatus.DRAFT,
                         created_at="2026-02-01T09:25:00+00:00",
                         body=("This loop is unbounded — a contended fleet busy-waits. Could we keep "
                               "letting `Contended` propagate and leave the backoff to "
                               "`retry_reserve`? `tests/test_contention.py` expects that too.")),
        ],
        threads=[
            ReviewThread(id="t1", file="scheduler/config.py", line=12, resolved=False,
                         capabilities={"reply": True, "resolve": True},
                         comments=[ThreadComment(id="tc1", author="ana", created_at="2026-02-01T08:40:00+00:00",
                                                 body="120s is four times the old value — deliberate?")]),
        ],
        access_requests=[
            AccessRequest(id="a1", repo="platform/virtu/fleet-api", status="pending",
                          reason="`Fleet` is defined there; the capacity check mirrors it"),
        ],
    )


# --- the shots -----------------------------------------------------------------

@shot("hub", "the landing page: what is open, and what is waiting on you", shows=".land",
      marks=(("#ref", "1"), (".land .hubhdr", "2"), (".land .queuehdr", "3")), height=620)
def _hub(page, base, stage):
    stage(showcase())
    page.goto(base)
    page.wait_for_selector(".land, #diff")
    page.wait_for_timeout(400)


@shot("diff", "the three panels a change is read in",
      shows="table.hunk .add",
      marks=(("#files", "1"), ("#diff", "2"), ("#rail", "3"),
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


@shot("claude-channel", "what Claude found on a line, and the conversation under it",
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
      shows="#railseg .btn",
      marks=(("#railseg", "1"), (".railpin .hrow.mr", "2"), (".rbar, .reviewbar", "3")))
def _review_state(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.wait_for_selector("#railseg .btn")
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
      shows=".railpin .railinsights .chip.crit",
      marks=((".passrow .btn", "1"), (".railpin .railinsights .chip.crit", "2"),
             (".railpin .themes", "3")))
def _insights(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.wait_for_selector(".railpin .railinsights .hrow")
    page.wait_for_timeout(300)


@shot("consent", "Claude asking to read another repository, and nothing read until you answer",
      shows=".rail .req button",
      marks=((".rail .req .why", "1"), (".rail .req button", "2")))
def _consent(page, base, stage):
    stage(showcase())
    _open(page, base)
    page.locator(".rail .req").first.scroll_into_view_if_needed()
    page.wait_for_timeout(300)


@shot("self-review", "reviewing your own branch: no merge request, and fixes landing as you comment",
      shows=".chip.fixed",
      marks=(("#mr", "1"), (".chip.fixed", "2")))
def _self_review(page, base, stage):
    stage(branch_review())
    _open(page, base)


def branch_review(session_id: str = "s1") -> SessionState:
    """The same review, of a branch that has not left this machine — no merge request to post to,
    and the agent answering by changing the code rather than explaining it."""
    state = showcase(session_id)
    state.mr = MRMetadata(
        host="local", project="control-plane", iid=0,
        title="reserve scheduler capacity per fleet", source_branch="feat/fleet-capacity",
        target_branch="main", sha="9f3c1ab", author="you",
        url="/home/you/src/control-plane", clone_url="/home/you/src/control-plane",
        capabilities={"threads": False, "approvals": False, "commits": True,
                      "diff_versions": False},
        diff_refs={"base_sha": "0ldbase", "head_sha": "9f3c1ab"})
    state.threads = []
    state.access_requests = []
    state.drafts = []
    state.addressed = [
        Addressed(subject=Subject(kind=SubjectKind.HIGHLIGHT, id="h1"), sha="9f3c1ab",
                  summary="bounded the retry at five attempts", at="2026-02-01T09:40:00+00:00"),
    ]
    state.messages = state.messages + [
        ChatMessage(id="m3", role="user", anchor=Subject(kind=SubjectKind.HIGHLIGHT, id="h1"),
                    body="bound it at five and let Contended through after that",
                    created_at="2026-02-01T09:38:00+00:00"),
        ChatMessage(id="m4", role="agent", anchor=Subject(kind=SubjectKind.HIGHLIGHT, id="h1"),
                    body=("Done — five attempts, then it propagates. `tests/test_contention.py` "
                          "passes unchanged now."),
                    created_at="2026-02-01T09:40:00+00:00"),
    ]
    return state


def _open(page, base):
    page.goto(f"{base}/?s=s1")          # `s` is the parameter the page reads
    page.wait_for_selector("table.hunk tr")
    page.wait_for_timeout(500)


# --- the harness ---------------------------------------------------------------

def main() -> int:
    IMAGES.mkdir(parents=True, exist_ok=True)
    manager = FakeManager()
    app = create_app(manager=manager, provider=HOST, with_mcp=False,
                     resolve_ref=lambda raw: MRRef(host="gitlab", project="p", iid=1))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise SystemExit("the screenshot server did not start")
        time.sleep(0.02)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    def stage(state):
        manager.reset()
        HOST.queue = list(QUEUE)
        HOST.blame_lines = [{"author": "ana", "date": "2026-01-14", "sha": "4c1f0b2",
                             "summary": "split the queue per fleet"}]
        HOST.issues = [{"iid": 402, "title": "scheduler starves the legacy fleet",
                        "url": "https://gitlab.example/issues/402"}]
        app.state.rail_scope.reset()
        manager.put(state)

    written = []
    with sync_playwright() as pw:
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
    server.should_exit = True
    thread.join(timeout=5)

    for name, description, size in written:
        print(f"  docs/images/{name}.png  {size // 1024:>4} kB  — {description}")
    print(f"{len(written)} screenshots written to {IMAGES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
