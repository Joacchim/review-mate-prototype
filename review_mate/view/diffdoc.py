"""Parse a unified diff into the document a client renders.

The host hands back raw diff text per file. Turning that into hunks and numbered lines is a
derivation, so it happens once here rather than in each client — two parsers would eventually
disagree about a line number, and a line number is what a comment is anchored to.

Tokenizing follows the same reasoning but has an extra constraint: a construct spanning several
lines only keeps its kind if the lexer sees them together. So each side of the diff is reassembled
into its own document — context plus deletions for the old side, context plus additions for the
new — lexed whole, and the spans handed back to the lines they came from.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from review_mate.view.tokens import tokenize

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")

CONTEXT, ADDED, REMOVED = "context", "added", "removed"


class DiffLine(BaseModel):
    side: str                      # context | added | removed
    old: int | None = None
    new: int | None = None
    text: str = ""
    tokens: list[list] = Field(default_factory=list)


class Hunk(BaseModel):
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    heading: str = ""
    gap_before: int = 0            # unchanged lines between the previous hunk and this one
    lines: list[DiffLine] = Field(default_factory=list)


FILE_HEADER = "diff --git "
# an added or removed *empty* file has no ---/+++ lines at all, so its name is only in the header.
# The backreference keeps a path containing spaces in one piece for the usual same-path case.
FILE_HEADER_PATHS = re.compile(r"^diff --git a/(.+) b/\1$")


class SplitFile(BaseModel):
    """One file's slice of a multi-file diff, with what its headers said about it."""
    path: str = ""
    old_path: str | None = None
    change_type: str = "modified"     # modified | added | deleted | renamed
    text: str = ""


def split_files(diff_text: str) -> list[SplitFile]:
    """Split a multi-file unified diff into one entry per file.

    A `diff --git` line only begins a file when the walk is not inside a hunk body. Diff *content*
    can contain that string — this repository's own tests do — and matching on it blindly cuts a
    hunk in half. Hunk bodies are bounded by the counts in their @@ header, which is what makes
    "inside a body" answerable without guessing.

    The path is read from the `+++ b/…` line, falling back to `--- a/…` for a deletion and to the
    `diff --git` header itself for an added or removed empty file, which carries neither. A rename
    keeps both ends, because a move is shown as a path divergence and needs them.
    """
    files: list[tuple[SplitFile, list[str]]] = []
    current: list[str] | None = None
    meta = SplitFile()
    taken_old = taken_new = want_old = want_new = 0
    inside = False

    def flush() -> None:
        if current is None:
            return
        if not meta.path and current:
            header_paths = FILE_HEADER_PATHS.match(current[0])
            if header_paths:
                meta.path = header_paths.group(1)
        if not meta.path and meta.old_path:      # a deletion: the new side was /dev/null
            meta.path = meta.old_path
        if meta.old_path == meta.path:
            meta.old_path = None
        files.append((meta, current))

    for raw in (diff_text or "").split("\n"):
        if not inside and raw.startswith(FILE_HEADER):
            flush()
            current, meta = [], SplitFile()
            header_paths = FILE_HEADER_PATHS.match(raw)
            if header_paths:
                meta.old_path = meta.path = header_paths.group(1)
            taken_old = taken_new = want_old = want_new = 0
            current.append(raw)
            continue
        if current is None:
            continue
        header = HUNK_HEADER.match(raw)
        if header:
            _, old_count, _, new_count, _ = header.groups()
            want_old, want_new = int(old_count or 1), int(new_count or 1)
            taken_old = taken_new = 0
            inside = True
            current.append(raw)
            continue
        if inside:
            if raw.startswith("\\"):
                current.append(raw)
                continue
            marker = raw[:1] if raw else " "
            if marker == "+":
                taken_new += 1
            elif marker == "-":
                taken_old += 1
            elif marker == " ":
                taken_old += 1
                taken_new += 1
            else:
                inside = False        # not diff body — the hunk ended early
                continue
            current.append(raw)
            if taken_old >= want_old and taken_new >= want_new:
                inside = False
            continue
        if raw.startswith("new file"):
            meta.change_type = "added"
        elif raw.startswith("deleted file"):
            meta.change_type = "deleted"
        elif raw.startswith("rename from "):
            meta.old_path, meta.change_type = raw[len("rename from "):], "renamed"
        elif raw.startswith("rename to "):
            meta.path, meta.change_type = raw[len("rename to "):], "renamed"
        elif raw.startswith("+++ ") and not raw.endswith("/dev/null"):
            meta.path = raw[4:].split("\t")[0].removeprefix("b/")
        elif raw.startswith("--- ") and not raw.endswith("/dev/null"):
            meta.old_path = raw[4:].split("\t")[0].removeprefix("a/")
        current.append(raw)
    flush()
    for entry, body in files:
        entry.text = "\n".join(body)
    return [entry for entry, _ in files]


def parse(diff_text: str) -> list[Hunk]:
    """Hunks with per-line old/new numbering, for **one file's** diff. Untokenized — see
    `tokenize_hunks`.

    The @@ header declares how many lines each side of the hunk holds, and the body is taken
    against those counts. Diff text conventionally ends in a newline, so splitting it yields a
    trailing empty element that would otherwise read as one more context line — and any hunk whose
    body is followed by trailing output would absorb it.
    """
    hunks: list[Hunk] = []
    current: Hunk | None = None
    old_line = new_line = 0
    taken_old = taken_new = 0
    for raw in (diff_text or "").split("\n"):
        header = HUNK_HEADER.match(raw)
        if header:
            old_start, old_count, new_start, new_count, heading = header.groups()
            current = Hunk(old_start=int(old_start), old_count=int(old_count or 1),
                           new_start=int(new_start), new_count=int(new_count or 1),
                           heading=heading.strip())
            if hunks:
                previous = hunks[-1]
                current.gap_before = max(current.new_start - (previous.new_start + previous.new_count), 0)
            else:
                current.gap_before = max(current.new_start - 1, 0)
            hunks.append(current)
            old_line, new_line = current.old_start, current.new_start
            taken_old = taken_new = 0
            continue
        if current is None:
            continue               # file headers (---, +++, diff --git) carry nothing to render
        if raw.startswith("\\"):
            continue               # "\ No newline at end of file"
        if taken_old >= current.old_count and taken_new >= current.new_count:
            continue               # this hunk is complete; anything after it is not its body
        marker, text = (raw[:1], raw[1:]) if raw else (" ", "")
        if marker == "+":
            current.lines.append(DiffLine(side=ADDED, new=new_line, text=text))
            new_line += 1
            taken_new += 1
        elif marker == "-":
            current.lines.append(DiffLine(side=REMOVED, old=old_line, text=text))
            old_line += 1
            taken_old += 1
        elif marker == " ":
            current.lines.append(DiffLine(side=CONTEXT, old=old_line, new=new_line, text=text))
            old_line += 1
            new_line += 1
            taken_old += 1
            taken_new += 1
        # any other leading character is not diff body — a trailing blank from the split, say
    return hunks


def tokenize_hunks(hunks: list[Hunk], path: str | None, language: str | None = None) -> None:
    """Attach token spans to every line, in place.

    Each side is lexed as one document so multi-line constructs survive, but a hunk is a window
    onto a file rather than the whole of it: a string opened before the hunk starts is invisible
    here, and the first lines may be lexed as if at top level. That is the same limitation the
    rendered diff itself has, and unfolding context narrows it.
    """
    for hunk in hunks:
        for side_marker, members in ((ADDED, (CONTEXT, ADDED)), (REMOVED, (CONTEXT, REMOVED))):
            lines = [line for line in hunk.lines if line.side in members]
            if not lines:
                continue
            spans = tokenize("\n".join(line.text for line in lines), path, language)
            for line, line_spans in zip(lines, spans):
                # a context line belongs to both documents and is lexed twice — identical input,
                # identical output, so the second pass is a harmless overwrite
                if line.side == side_marker or line.side == CONTEXT:
                    line.tokens = line_spans


def build(diff_text: str, path: str | None, language: str | None = None) -> list[Hunk]:
    hunks = parse(diff_text)
    tokenize_hunks(hunks, path, language)
    return hunks
