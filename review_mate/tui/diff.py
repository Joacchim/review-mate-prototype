"""The review screen: a file list and one file's diff, rendered from the `diff` scopes.

This is where the protocol earns the split. The screen subscribes to the file list and, separately,
to whichever file is open — so moving between files changes what it watches rather than what it
computes, and nothing here parses a diff or decides what a line means.

Token kinds arrive as semantics and are coloured here, because a terminal palette is this client's
business and no one else's. A kind with no entry renders plain, which is how the vocabulary can
grow without this file changing.
"""
from __future__ import annotations

from review_mate.tui.client import ViewClient

KIND_STYLE = {
    "keyword": "#c678dd",
    "string": "#98c379",
    "docstring": "#98c379",
    "comment": "#7f848e",
    "number": "#d19a66",
    "function": "#61afef",
    "class": "#e5c07b",
    "type": "#e5c07b",
    "decorator": "#c678dd",
    "builtin": "#56b6c2",
    "constant": "#d19a66",
    "namespace": "#e5c07b",
    "tag": "#e06c75",
    "attribute": "#d19a66",
    "operator": "#56b6c2",
    "deleted": "#e06c75",
    "inserted": "#98c379",
    "heading": "#61afef",
}

SIDE_BASE = {"added": "bg:#1d2b1d", "removed": "bg:#2e1d1d", "context": ""}
SIDE_MARK = {"added": "+", "removed": "-", "context": " "}

MODES = ("full", "since")
FILE_PANE_ROWS = 8


def line_fragments(text: str, spans, base: str) -> list[tuple[str, str]]:
    """Apply token spans to one line, leaving the gaps between them in the base style."""
    if not spans:
        return [(base, text)]
    out: list[tuple[str, str]] = []
    cursor = 0
    for start, length, kind in sorted(spans, key=lambda span: span[0]):
        start = max(start, cursor)
        end = min(start + length, len(text))
        if end <= start:
            continue
        if start > cursor:
            out.append((base, text[cursor:start]))
        colour = KIND_STYLE.get(kind)
        out.append((f"{base} {colour}".strip() if colour else base, text[start:end]))
        cursor = end
    if cursor < len(text):
        out.append((base, text[cursor:]))
    return out


class DiffScreen:
    def __init__(self, client: ViewClient, session: str, mode: str = "full") -> None:
        self.client = client
        self.session = session
        self.mode = mode
        self.file_index = 0
        self.scroll = 0
        self.focus = "files"          # files | body
        self.rows = 24

    # --- what it watches --------------------------------------------------

    @property
    def listing(self) -> str:
        return f"diff:{self.session}:{self.mode}"

    @property
    def files(self) -> list[dict]:
        return (self.client.views.get(self.listing) or {}).get("files", [])

    @property
    def current(self) -> dict | None:
        rows = self.files
        if not rows:
            return None
        self.file_index = max(0, min(self.file_index, len(rows) - 1))
        return rows[self.file_index]

    @property
    def body_scope(self) -> str | None:
        row = self.current
        # a file's scope name is the list's name with the path appended — nothing to assemble
        return f"{self.listing}:{row['path']}" if row else None

    def wanted(self) -> list[str]:
        scopes = [self.listing]
        body = self.body_scope
        if body:
            scopes.append(body)
        return scopes

    # --- rendering ---------------------------------------------------------

    def fragments(self) -> list[tuple[str, str]]:
        view = self.client.views.get(self.listing)
        out: list[tuple[str, str]] = []
        if view is None:
            return [("class:muted", " loading the change…\n")]
        mr = view.get("mr") or {}
        title = f"{mr.get('project', '')}!{mr.get('iid', '')}  {mr.get('title', '')}"
        out.append(("class:header", f" {title}\n"))
        out.append(("class:muted", f"  mode {self.mode}   [{self.client.status}]"))
        if not view.get("head_aligned", True):
            out.append(("class:attention", "   read-only: the MR moved past this session"))
        out.append(("", "\n"))
        state = view.get("state", "ready")
        if state != "ready":
            note = {"loading": "resolving…", "unavailable": "this host cannot serve that mode",
                    "error": view.get("error", ""), "unsupported-mode": "mode not supported",
                    "unknown-session": "this review is not open"}.get(state, state)
            out.append(("class:error" if state == "error" else "class:muted", f"\n  {note}\n"))
            out.append(("class:footer", self._footer()))
            return out
        out.extend(self._file_pane())
        out.append(("", "\n"))
        out.extend(self._body_pane())
        out.append(("class:footer", self._footer()))
        return out

    def _file_pane(self) -> list[tuple[str, str]]:
        rows = self.files
        if not rows:
            return [("class:muted", "  no files in this view\n")]
        out = []
        top = max(0, min(self.file_index - FILE_PANE_ROWS // 2, len(rows) - FILE_PANE_ROWS))
        for index in range(top, min(top + FILE_PANE_ROWS, len(rows))):
            row = rows[index]
            selected = index == self.file_index
            marker = "›" if selected else " "
            counts = f"+{row.get('additions', 0)} -{row.get('deletions', 0)}"
            style = "class:selected" if selected and self.focus == "files" else ""
            out.append(("class:muted", f" {marker} {counts:>9}  "))
            out.append((style, f"{row.get('path', '')}\n"))
        if len(rows) > FILE_PANE_ROWS:
            out.append(("class:muted", f"   … {len(rows)} files\n"))
        return out

    def _body_pane(self) -> list[tuple[str, str]]:
        scope = self.body_scope
        if scope is None:
            return []
        view = self.client.views.get(scope)
        if view is None:
            return [("class:muted", "  loading the file…\n")]
        state = view.get("state", "ready")
        if state != "ready":
            return [("class:muted", f"  {view.get('error') or state}\n")]
        lines = list(self._body_lines(view))
        height = max(self.rows - FILE_PANE_ROWS - 7, 4)
        self.scroll = max(0, min(self.scroll, max(len(lines) - height, 0)))
        out: list[tuple[str, str]] = []
        for fragment_line in lines[self.scroll:self.scroll + height]:
            out.extend(fragment_line)
        return out

    def _body_lines(self, view):
        for hunk in view.get("hunks", []):
            if hunk.get("gap_before"):
                yield [("class:muted", f"  ⋯ {hunk['gap_before']} unchanged lines ⋯\n")]
            heading = f"  @@ {hunk.get('heading', '')}\n"
            yield [("class:hunk", heading)]
            for line in hunk.get("lines", []):
                side = line.get("side", "context")
                base = SIDE_BASE.get(side, "")
                gutter = f"{str(line.get('old') or ''):>5}{str(line.get('new') or ''):>6} "
                pieces = [("class:muted", gutter), (base, SIDE_MARK.get(side, " "))]
                pieces.extend(line_fragments(line.get("text", ""), line.get("tokens", []), base))
                pieces.append((base, "\n"))
                yield pieces

    def _footer(self) -> str:
        return ("\n tab pane   j/k move   n/p file   m mode   b back   q quit\n"
                if self.focus == "files"
                else "\n tab pane   j/k scroll   n/p file   m mode   b back   q quit\n")

    # --- interaction ---------------------------------------------------------

    def move(self, delta: int) -> None:
        if self.focus == "files":
            rows = self.files
            if rows:
                self.file_index = max(0, min(self.file_index + delta, len(rows) - 1))
                self.scroll = 0
        else:
            self.scroll = max(0, self.scroll + delta)

    def next_file(self, delta: int) -> None:
        rows = self.files
        if rows:
            self.file_index = max(0, min(self.file_index + delta, len(rows) - 1))
            self.scroll = 0

    def toggle_focus(self) -> None:
        self.focus = "body" if self.focus == "files" else "files"

    def cycle_mode(self) -> str:
        """Switching mode is a subscription, not a command — the name changes and the server
        answers for the new one."""
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)] if self.mode in MODES else "full"
        self.scroll = 0
        return self.mode
