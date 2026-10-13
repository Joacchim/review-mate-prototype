"""Short clips that answer "how do I do X?" — one per feature the documentation explains.

    uv run --extra webtest python tools/clips.py                 # all of them
    uv run --extra webtest python tools/clips.py --only writing  # one, while working on it
    uv run --extra webtest python tools/clips.py --speed 1.6     # slower, for a close reading
    uv run --extra webtest python tools/clips.py --list          # what exists and what it answers

Driven against the real application, the same way the stills are: a clip cannot show a gesture the
product does not have, and when the product changes the clip changes with it or the run fails.

## Changing one

A clip is a short script read top to bottom. `c.say` puts a line on screen, `c.click` moves a
pointer there and presses, `c.type` writes, `c.beat` waits. Every one of those is measured in
beats rather than milliseconds, and `--speed` multiplies all of them at once — so making a clip
easier to follow is a flag, not an edit.

Change what the clip *does* by editing its function below. Change what the review it happens in
*contains* — the diff, the comments, who said what — in `tools/_stage.py`, which the stills read
from too, so the two cannot drift into documenting different products.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from _stage import (  # noqa: E402
    HOST, IMAGES, ChangeType, FileEntry, branch_review, part_read, serving, showcase,
    sync_playwright,
)

CLIPS_DIR = IMAGES / "clips"

# --- timing -------------------------------------------------------------------
# One beat is the unit everything else is written in, so a clip reads as "wait a moment" rather
# than as a pile of millisecond constants nobody dares change. `--speed` scales the lot.
BEAT_MS = 420
MOVE_MS = 950          # the pointer travelling — slow enough to be followed, which is
                       # the whole job of drawing one
CLICK_MS = 360         # after a press, before the next thing — the UI has to be seen reacting
TYPE_MS = 55           # per character
READ_MS = 950          # a caption's own time, before whatever it introduces happens
HOLD_MS = 1900         # the last frame, so the result is actually looked at

VIEWPORT = {"width": 1180, "height": 740}
FPS = 10
WIDTH = 900            # the gif's width; the page is recorded larger and scaled down to it
COLORS = 64            # a UI is flat colour, so a palette this size costs nothing visible

CLIPS: list = []


def clip(name: str, asks: str, shows: str, dim: float = 0.0):
    """Register a clip.

    `asks` is the question it answers, in the reader's words — it becomes the caption in the docs,
    and writing it first is what keeps a clip from becoming a tour of the screen.

    `shows` is a selector that must be on screen when the last frame is taken. Without it a clip
    that silently stopped working still produces a file, and a file is what the documentation
    trusts.

    `dim` takes the brightness down a little, for a clip that is mostly a white screen. A full
    page of light theme at full strength is unpleasant to have autoplay in a document, and the
    picture is still the product's — only turned down, the way a screen is.
    """
    def register(fn):
        CLIPS.append((name, asks, shows, dim, fn))
        return fn
    return register


# --- what a clip can do -------------------------------------------------------

_OVERLAY_JS = """
() => {
  if (document.getElementById('__clipui')) return;
  const layer = document.createElement('div');
  layer.id = '__clipui';
  layer.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none';
  // Starts in the middle of the screen rather than a corner: a pointer that enters from off-frame
  // is already moving by the time it is noticed, and the first gesture of a clip is the one most
  // worth following.
  layer.innerHTML = `
    <div id="__cursor" style="
      position:fixed;left:50vw;top:50vh;width:22px;height:22px;border-radius:50%;
      background:rgba(229,72,77,.3);border:2.5px solid #e5484d;
      box-shadow:0 0 0 4px rgba(229,72,77,.14), 0 2px 10px rgba(0,0,0,.3);
      transform:translate(-50%,-50%) scale(2.4);opacity:0;
      transition:left MOVEms cubic-bezier(.33,.02,.2,1),top MOVEms cubic-bezier(.33,.02,.2,1),
                 transform .45s ease-out,opacity .3s"></div>`;
  document.body.appendChild(layer);
}
"""

# Grows as it sets off and settles as it arrives, so where it is going is legible before it gets
# there — a dot of constant size crossing a screenshot reads as a speck of dust.
_POINT_JS = """
([x, y]) => {
  const cur = document.getElementById('__cursor');
  if (!cur) return;
  cur.style.opacity = '1';
  cur.style.transform = 'translate(-50%,-50%) scale(2.4)';
  requestAnimationFrame(() => {
    cur.style.left = x + 'px';
    cur.style.top = y + 'px';
    setTimeout(() => { cur.style.transform = 'translate(-50%,-50%) scale(1)'; }, 90);
  });
}
"""

# A press has to be visible or the clip looks like the UI acting on its own.
_PRESS_JS = """
() => {
  const cur = document.getElementById('__cursor');
  if (!cur) return;
  const ring = document.createElement('div');
  ring.style.cssText =
    `position:fixed;left:${cur.style.left};top:${cur.style.top};width:22px;height:22px;` +
    'border-radius:50%;border:2.5px solid #e5484d;transform:translate(-50%,-50%);' +
    'transition:width .42s,height .42s,opacity .42s;opacity:.9';
  document.getElementById('__clipui').appendChild(ring);
  requestAnimationFrame(() => {
    ring.style.width = '64px'; ring.style.height = '64px'; ring.style.opacity = '0';
  });
  setTimeout(() => ring.remove(), 500);
}
"""


class Cue:
    """The one surface a clip is written against.

    Every wait goes through `_wait`, so `--speed` reaches all of them — including the ones inside
    `click` and `type`, which is what makes slowing a clip down a flag rather than an edit.
    """

    def __init__(self, page, base, stage, speed: float):
        self.page = page
        self.base = base
        self._stage = stage
        self.speed = speed
        self.steps: list[str] = []      # what `say` would have shown, for the prose above the clip
        self._frames: Path | None = None   # where this clip's step stills go, if any are wanted
        self._pending = False              # a step is open and has not had its picture taken
        self._last_shot: bytes | None = None   # so a step that changed nothing writes nothing

    # --- setting the scene ---
    def stage(self, state):
        self._stage(state)

    def open(self, session: str = "s1", params: str = "") -> None:
        self.page.goto(f"{self.base}/?s={session}{params}")
        self.page.wait_for_selector("table.hunk tr, .land", timeout=15000)
        self._overlay()
        self._wait(BEAT_MS)

    def _overlay(self) -> None:
        """The pointer, with its travel time matched to the clip's. `--speed` slows the gesture as
        well as the pauses, or a slowed-down clip is the same dart between longer silences."""
        self.page.evaluate(_OVERLAY_JS.replace("MOVEms", f"{MOVE_MS * self.speed / 1000:.2f}s"))

    def land(self) -> None:
        """The landing page rather than a review — where finding something to read starts."""
        self.page.goto(self.base)
        self.page.wait_for_selector(".land", timeout=15000)
        self._overlay()
        self._wait(BEAT_MS)

    # --- what the reader is told ---
    def say(self, text: str) -> None:
        """A step, in words. Not drawn on the clip.

        A caption burned into the picture is unreadable at the size a reader scrolls past, cannot
        be copied, searched or translated, and has to be re-recorded to fix a typo. So the words
        go above the clip as prose and this only paces it — the pause a caption needed to be read
        is the pause that makes the gesture after it legible.
        """
        self._capture()                 # the step that was running, now that it has finished
        self.steps.append(text)
        self._pending = True
        self._wait(READ_MS + 26 * len(text))

    # --- what the reader is shown ---
    def click(self, selector: str, nth: int = 0) -> None:
        element = self.page.locator(selector).nth(nth)
        element.wait_for(state="visible", timeout=10000)
        element.scroll_into_view_if_needed()
        box = element.bounding_box()
        if box is None:
            raise SystemExit(f"nothing to press at {selector!r} — the clip would click thin air")
        self.page.evaluate(_POINT_JS, [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
        self._wait(MOVE_MS)
        self.page.evaluate(_PRESS_JS)
        element.click()
        self._wait(CLICK_MS)

    def type(self, selector: str, text: str) -> None:
        element = self.page.locator(selector).first
        element.wait_for(state="visible", timeout=10000)
        box = element.bounding_box()
        if box is not None:
            self.page.evaluate(_POINT_JS,
                               [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
            self._wait(MOVE_MS // 2)
        element.click()
        element.type(text, delay=TYPE_MS * self.speed)
        self._wait(CLICK_MS)

    def drag_lines(self, first: int, last: int) -> None:
        """Mark a range the way a reviewer does — press on one line and release on another."""
        start = self.page.locator(f'td.code[data-line="{first}"]').first
        end = self.page.locator(f'td.code[data-line="{last}"]').first
        start.wait_for(state="visible", timeout=10000)
        a, b = start.bounding_box(), end.bounding_box()
        self.page.evaluate(_POINT_JS, [a["x"] + 40, a["y"] + a["height"] / 2])
        self._wait(MOVE_MS)
        self.page.mouse.move(a["x"] + 40, a["y"] + a["height"] / 2)
        self.page.mouse.down()
        self.page.evaluate(_POINT_JS, [b["x"] + 120, b["y"] + b["height"] / 2])
        self._wait(MOVE_MS)
        self.page.mouse.move(b["x"] + 120, b["y"] + b["height"] / 2, steps=12)
        self.page.mouse.up()
        self._wait(CLICK_MS)

    def hover(self, selector: str) -> None:
        element = self.page.locator(selector).first
        element.wait_for(state="visible", timeout=10000)
        box = element.bounding_box()
        self.page.evaluate(_POINT_JS, [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
        self._wait(MOVE_MS)
        element.hover()
        self._wait(CLICK_MS)

    # --- time ---
    def beat(self, n: float = 1) -> None:
        self._wait(BEAT_MS * n)

    def hold(self, text: str | None = None) -> None:
        """The last frame, held long enough to be looked at. Its text is a step like any other."""
        if text:
            self._capture()
            self.steps.append(text)
            self._pending = True
        self._wait(HOLD_MS + (26 * len(text) if text else 0))

    def _wait(self, ms: float) -> None:
        self.page.wait_for_timeout(ms * self.speed)

    # --- the same steps, standing still ---------------------------------------
    # A clip answers "how do I" at the speed of someone who already knows. Somebody following
    # along needs to stop on a step, and a reader who cannot watch a moving picture at all needs
    # it to exist in another form — so each step is also a frame, taken from this same run rather
    # than from a second script that would drift from it.

    def _capture(self) -> None:
        """One still for the step that has just finished, named by its number.

        Not written when it would be the same picture as the step before. A step that is a remark
        rather than an action — "they go together when you send the review" — changes nothing on
        screen, and a second copy of the previous picture under a new number tells a reader
        following along that they missed something.
        """
        if not (self._frames and self._pending):
            return
        self._pending = False
        shot = self.page.screenshot()
        if shot == self._last_shot:
            return
        self._last_shot = shot
        self._frames.mkdir(parents=True, exist_ok=True)
        (self._frames / f"{len(self.steps)}.png").write_bytes(shot)

    def finish(self) -> None:
        """Take the last step's picture. The runner calls this; a clip never has to remember."""
        self._capture()


# --- the clips ----------------------------------------------------------------
# Each answers one question a reader actually arrives with. Written as the shortest path to the
# answer: a clip that tours the screen first is a clip nobody watches to the end.

@clip("finding", asks="How do I find something to review?", shows=".searchresults .qitem")
def _finding(c):
    c.stage(showcase())
    HOST.search_hits = [
        {"project": "platform/virtu/control-plane", "iid": 137,
         "title": "reserve scheduler capacity per fleet", "url": "https://gitlab.example/mr/137"},
        {"project": "platform/virtu/control-plane", "iid": 92,
         "title": "retire the single queue", "url": "https://gitlab.example/mr/92"},
    ]
    c.land()
    c.say("What is waiting on you is already here")
    c.beat()
    c.say("Anything else is found by name")
    c.type("#ref", "capacity")
    c.page.wait_for_selector(".searchresults .qitem", timeout=10000)
    c.hold("Track flags one for later without leaving the list")


@clip("reading", asks="How do I read the change?", shows="table.hunk tr")
def _reading(c):
    c.stage(showcase())
    c.open()
    c.say("Pick a file on the left")
    c.click(".node.file", nth=1)
    c.say("Open the lines a hunk left out")
    c.click("tr.expand .exlink")
    c.beat()
    c.say("Side by side, when a rewrite reads better that way")
    c.click("#t-split")
    c.hold("The same change, laid out to suit it")


@clip("asking", asks="How do I ask Claude about a line?", shows="#detail .msgs")
def _asking(c):
    c.stage(showcase())
    c.open()
    c.say("Drag across the lines you are unsure about")
    c.drag_lines(41, 43)
    c.say("Marking collects them — it asks nobody anything yet")
    c.beat()
    c.say("Open the row, and ask")
    c.click("#hlist .hrow")
    c.beat()
    c.type("#detail .chatbox input", "why is the legacy queue reachable?")
    c.page.keyboard.press("Enter")
    c.hold("The answer arrives on this subject, where you asked")


@clip("writing", asks="How do I write a review comment?", shows=".draft textarea")
def _writing(c):
    c.stage(showcase())
    c.open()
    c.click("#hlist .hrow")
    c.say("The Review tab is the comment you are preparing")
    c.click("#detail .tab", nth=1)
    c.beat()
    c.type(".draft textarea.draftbox", "this retries without ever sleeping")
    c.say("Save keeps it; nothing is sent one comment at a time")
    c.click(".draftbtns .btn")
    c.hold("They go together when you send the review")


@clip("marking-read", asks="How do I keep track of what I have already read?",
      shows=".node.file.reviewed .rv")
def _marking_read(c):
    c.stage(part_read())
    c.open()
    c.say("Mark a file read when you are done with it")
    c.click(".fname .rvbtn")
    c.beat()
    c.say("The tree ticks it, and the count says how far through you are")
    c.hover(".treeprog")
    c.hold("A tick goes grey if the author changes that file again")


@clip("per-commit", asks="How do I read a change one commit at a time?", shows=".commitbar")
def _per_commit(c):
    state = showcase()
    state.mr.capabilities = {**state.mr.capabilities, "commits": True}
    c.stage(state)
    HOST.commit_list = [
        {"sha": "9f3c1ab", "short_id": "9f3c1ab",
         "title": "reserve per fleet, falling back to legacy", "message": ""},
        {"sha": "abc123def", "short_id": "abc123d", "title": "bound the retry loop", "message": ""},
    ]
    HOST.commit_files = {"9f3c1ab": [
        FileEntry(path="scheduler/capacity.py", change_type=ChangeType.MODIFIED,
                  hunks=[{"diff": "@@ -38,4 +38,6 @@\n-        queue = self._queues[pu.fleet]\n"
                                  "+        queue = self._queues.get(pu.fleet)\n"
                                  "+        if queue is None:\n"
                                  "+            queue = self._legacy\n"}])]}
    c.open()
    c.say("Step through the change the way it was written")
    c.click("#t-commits")
    c.beat(2)
    c.hold("Oldest first, and lines a later commit writes over are marked as you read")


@clip("self-review", asks="How do I review my own branch before anyone sees it?",
      shows=".chip.fixed")
def _self_review(c):
    c.stage(branch_review())
    c.open()
    c.say("A branch on this machine reads the same way")
    c.hover("#mr")
    c.say("There is no merge request, so comments go to Claude instead")
    c.beat()
    c.hold("…and what you asked for is marked as it lands")


@clip("theme", asks="How do I switch between light and dark?", shows="#t-theme",
      dim=0.07)   # most of this one is a full page of light theme, which glares
def _theme(c):
    c.stage(showcase())
    c.open()
    c.say("One control, and it is remembered on this machine")
    c.click("#t-theme")
    c.beat(2)
    c.hold()


@clip("help", asks="How do I find out what something on screen does?", shows="#t-help.on")
def _help(c):
    c.stage(showcase())
    c.open()
    c.say("Turn the question mark on…")
    c.click("#t-help")
    c.beat()
    c.say("…then point at whatever you are unsure of")
    c.hover("#t-commits")
    c.beat(2)
    c.hover(".treehdr")
    c.hold("Press it again, or Escape, to put them away")


# --- recording ----------------------------------------------------------------

def _to_gif(webm: Path, gif: Path, fps: int, width: int, dim: float = 0.0) -> None:
    """webm out of the browser, GIF into the documentation.

    GIF because that is what renders inline wherever the docs are read — a merge request, a
    README on a forge, a file open in an editor. A clip nobody can see is worse than the still it
    replaced, and the better formats are exactly the ones a markdown viewer declines to play.

    Two passes over one palette: a UI is flat colour, so a palette built from the clip itself is
    both smaller and sharper than the default web one, which dithers text into mush.
    """
    # Most of a clip is the same picture: a reviewer reads a caption, nothing moves. `mpdecimate`
    # drops those repeats and the encoder lengthens the delay on the frame they followed, so the
    # clip plays at the speed it was recorded at out of a fifth of the frames. The threshold is
    # low on purpose — a pointer crossing the screen changes very little of it, and a brisker
    # setting throws the gesture away, which is the one thing a clip is for.
    #
    # `final_delay` because the last frame has no following gap to stretch into, so without it the
    # closing shot flashes past and the loop restarts on top of the thing it was meant to show.
    soften = f"eq=brightness=-{dim:.3f}," if dim else ""
    chain = (f"{soften}fps={fps},mpdecimate=hi=5000:lo=2000:frac=0.002,"
             f"scale={width}:-1:flags=lanczos,split[a][b];"
             f"[a]palettegen=max_colors={COLORS}:stats_mode=diff[p];"
             f"[b][p]paletteuse=dither=none")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(webm),
         "-filter_complex", chain, "-loop", "0", "-final_delay", "200", str(gif)],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg could not make {gif.name}: {result.stderr.strip()[:300]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", default=[],
                        help="record just these, by name — while working on one")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="multiply every wait: 1.6 is noticeably slower, 0.7 brisker")
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--width", type=int, default=WIDTH,
                        help="the gif's width in pixels — smaller is lighter in git")
    parser.add_argument("--height", type=int, default=VIEWPORT["height"])
    parser.add_argument("--list", action="store_true", help="what exists, and what each answers")
    parser.add_argument("--keep-webm", action="store_true",
                        help="leave the raw recording beside the gif, for a closer look")
    parser.add_argument("--no-frames", action="store_true",
                        help="skip the per-step stills — faster, while working on the motion")
    args = parser.parse_args()

    if args.list:
        for name, asks, _, _, _ in CLIPS:
            print(f"  {name:<14} {asks}")
        return 0

    chosen = [c for c in CLIPS if not args.only or c[0] in args.only]
    if not chosen:
        raise SystemExit(f"no clip called {args.only} — try --list")
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    size = {"width": VIEWPORT["width"], "height": args.height}
    written, raw = [], Path(tempfile.mkdtemp(prefix="clips-"))

    with serving() as (base, stage), sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, asks, shows, dim, script in chosen:
            context = browser.new_context(viewport=size, record_video_dir=str(raw),
                                          record_video_size=size)
            page = context.new_page()
            cue = Cue(page, base, stage, args.speed)
            frames = CLIPS_DIR / name
            if not args.no_frames:
                shutil.rmtree(frames, ignore_errors=True)   # a renumbered clip leaves no orphans
                cue._frames = frames
            try:
                script(cue)
                cue.finish()
                if not page.locator(shows).first.is_visible():
                    raise SystemExit(
                        f"{name}: nothing matching {shows!r} is on screen at the end, so the clip "
                        f"does not answer {asks!r} — fix the clip rather than the question")
                source = Path(page.video.path())
            finally:
                context.close()          # the recording is only written out here
            gif = CLIPS_DIR / f"{name}.gif"
            _to_gif(source, gif, args.fps, args.width, dim)
            if args.keep_webm:
                shutil.copy(source, CLIPS_DIR / f"{name}.webm")
            source.unlink(missing_ok=True)
            shots = sorted(frames.glob("*.png")) if frames.exists() else []
            written.append((name, asks, gif.stat().st_size, list(cue.steps), len(shots)))
        browser.close()
    shutil.rmtree(raw, ignore_errors=True)

    for name, asks, size_bytes, steps, shots in written:
        print(f"  docs/images/clips/{name}.gif  {size_bytes // 1024:>5} kB  "
              f"{shots} step stills  — {asks}")
        if shots > len(steps):
            raise SystemExit(f"{name}: {shots} stills for {len(steps)} steps — a reader following "
                             f"along would run out of words before they ran out of pictures")
    print(f"{len(written)} clips written to {CLIPS_DIR}")

    # The steps are the clip's own words and they belong above it, in prose a reader can search,
    # copy and translate. Printed rather than written into the documentation: where they go is a
    # judgement about the section they land in, and a tool that edits prose gets that wrong.
    print("\nSteps, for the prose above each clip — a test checks the documentation still says them:")
    for name, _, _, steps, _ in written:
        print(f"\n  {name}")
        for n, step in enumerate(steps, 1):
            print(f"    {n}. {step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
