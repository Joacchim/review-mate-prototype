# The pictures the documentation is made of

Two tools, one review. Both drive the real application — `create_app` over a staged session, the
arrangement the browser tests use — so neither can show a screen the product does not have.

| | |
|---|---|
| `tools/screenshots.py` | stills: what a screen **is** |
| `tools/clips.py` | clips: **how do I do X?**, one per feature |
| `tools/_stage.py` | the review they both happen in — the diff, the comments, who said what |

```
uv run --extra webtest python tools/screenshots.py
uv run --extra webtest python tools/clips.py
```

## Changing a clip

A clip is a short script read top to bottom:

```python
@clip("marking-read", asks="How do I keep track of what I have already read?",
      shows=".node.file.reviewed .rv")
def _marking_read(c):
    c.stage(part_read())
    c.open()
    c.say("Mark a file read when you are done with it")
    c.click(".fname .rvbtn")
    c.hold("A tick goes grey if the author changes that file again")
```

Each step is also photographed. The clip plays at the speed of somebody who already knows the
answer; the stills are the same steps standing still, in a folded section under the clip, for a
reader following along — and for one who cannot watch a moving picture at all, which is the half
that makes them more than a convenience. They come from the same run, so they cannot drift from
the clip, and a step that changes nothing on screen writes no still rather than a second copy of
the one before. `--no-frames` skips them while you are working on the motion.

`say` and `hold` are the clip's words, and they are **not drawn on it**. A caption burned into the
picture is unreadable at the size a reader scrolls past, cannot be searched or translated, and
needs a re-record to fix a typo — so they pace the clip and are printed after a run, to go above it
as a numbered list. A test checks the documentation still says them.

- **`asks`** is the question it answers, in the reader's words. Writing it first is what keeps a
  clip from becoming a tour of the screen.
- **`shows`** must be on screen at the last frame. Without it a clip that silently stopped working
  still writes a file, and a file is what the documentation trusts.
- **`c.say`** is a step in words, **`c.click`** moves a pointer there and presses it,
  **`c.type`** writes, **`c.drag_lines`** marks a range, **`c.beat`** waits, **`c.hold`** ends.

Every wait is counted in beats rather than milliseconds, and `--speed` multiplies all of them at
once — so making a clip easier to follow is a flag, not an edit:

```
uv run --extra webtest python tools/clips.py --only writing --speed 1.6
uv run --extra webtest python tools/clips.py --list
uv run --extra webtest python tools/clips.py --only asking --keep-webm   # look at it frame by frame
```

Change what the clip *does* in `tools/clips.py`. Change what the review it happens in *contains*
in `tools/_stage.py`, which the stills read from too — which is what stops the two documenting
slightly different products.

## Why GIF, and what it costs

GIF because it renders inline wherever the docs are read — a merge request, a forge's README, a
file open in an editor. The better formats are the ones a markdown viewer declines to play, and a
clip nobody can see is worse than the still it replaced.

The cost is weight. Most of a clip is the same picture — a reader is reading a caption, nothing is
moving — so `mpdecimate` drops the repeats and the encoder lengthens the delay on the frame they
followed, which is what turns four thousand kilobytes into seven hundred without changing the
speed it plays at. What survives that is motion, and the clips that type something are the heavy
ones, because every keystroke is a frame no decimator can drop.

`--width` and `--fps` are the two levers if the total needs to come down; legibility is what they
spend, so check a frame before taking either far.

`dim=` on a clip takes its brightness down a little. For one that is mostly a full page of light
theme, which glares when it autoplays in a document — the picture is still the product's, only
turned down the way a screen is.
