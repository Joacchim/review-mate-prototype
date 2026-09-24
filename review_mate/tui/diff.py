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
RAIL_PANE_ROWS = 5
CHAT_PANE_ROWS = 6
THREAD_PANE_ROWS = 5

# what the agent state reads as on one line — the server decides the word, this picks the colour
AGENT_STYLE = {"working": "class:info", "stalled": "class:error",
               "watching": "class:ok", "off": "class:muted"}
AGENT_LABEL = {"working": "Claude is on it", "stalled": "nothing is listening",
               "watching": "Claude is watching", "off": "no agent"}

# whether a line already has a comment prepared for it, or one already sent — the rail says so
# without the reviewer opening anything
COMMENT_MARK = {"comment": ("class:info", "✎ "), "posted": ("class:ok", "✓ ")}


def _one_line(body: str, width: int = 68) -> str:
    """A message as one row: the terminal shows the exchange, not the prose."""
    line = next((ln for ln in (body or "").splitlines() if ln.strip()), "")
    return line if len(line) <= width else line[: width - 1] + "\u2026"


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
        self.body_cursor = 0          # index into the rendered body rows
        self.rail_index = 0
        self.anchor: int | None = None   # a selection in progress, at this new-side line
        self.focus = "files"          # files | body | rail | threads
        self.thread_index = 0
        self.thread_filter = "unresolved"   # unresolved | all
        self.browsing = False         # the file list shows the whole repository, not just the diff
        self.viewing: str | None = None    # a repository file open as itself, rather than as a diff
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
        rows = self.browse_rows()
        if not rows:
            return None
        self.file_index = max(0, min(self.file_index, len(rows) - 1))
        return rows[self.file_index]

    @property
    def body_scope(self) -> str | None:
        row = self.current
        if row is None or not row.get("changed", True):
            return None        # a repository file is read from its blob, not from a diff
        # a file's scope name is the list's name with the path appended — nothing to assemble
        return f"{self.listing}:{row['path']}"

    @property
    def rail(self) -> dict:
        return self.client.views.get(f"rail:{self.session}") or {}

    @property
    def chat(self) -> dict:
        """The index: every conversation this review holds, and the state the agent is in."""
        return self.client.views.get(f"chat:{self.session}") or {}

    @property
    def tree(self) -> dict:
        """Every file in the repository at this change's sha, while the browser is open."""
        return self.client.views.get(f"tree:{self.session}") or {}

    def repo_paths(self) -> list[str]:
        view = self.tree
        return view.get("paths", []) if view.get("state") == "ready" else []

    def browse_rows(self) -> list[dict]:
        """The file list: what the change touched, then the rest of the repository under it.

        A changed file keeps its counts; a repository file is listed as itself. They are one list
        because the reviewer is picking a file, not picking a kind of file.
        """
        rows = [dict(f, changed=True) for f in self.files]
        if not self.browsing:
            return rows
        touched = {f["path"] for f in rows}
        rows.extend({"path": p, "changed": False}
                    for p in self.repo_paths() if p not in touched)
        return rows

    @property
    def blob(self) -> dict:
        return self.client.views.get(self.blob_scope) or {} if self.blob_scope else {}

    @property
    def blob_scope(self) -> str | None:
        return f"blob:{self.session}:{self.mode}:{self.viewing}" if self.viewing else None

    @property
    def access(self) -> dict:
        """What Claude has asked to read, and what was decided."""
        return self.client.views.get(f"access:{self.session}") or {}

    def pending_access(self) -> list[dict]:
        return [r for r in (self.access.get("requests") or []) if r.get("status") == "pending"]

    @property
    def review(self) -> dict:
        """What is prepared to send: the drafts, the approval, and whether this has moved on."""
        return self.client.views.get(f"review:{self.session}") or {}

    @property
    def threads(self) -> dict:
        """The discussions already on the merge request, as the host last reported them."""
        return self.client.views.get(f"threads:{self.session}") or {}

    def thread_rows(self) -> list[dict]:
        """What the filter leaves. `unresolved` is the one a reviewer reaches for, so it leads."""
        rows = self.threads.get("threads") or []
        return rows if self.thread_filter == "all" else [t for t in rows if not t.get("resolved")]

    def current_thread(self) -> dict | None:
        rows = self.thread_rows()
        if not rows:
            return None
        self.thread_index = max(0, min(self.thread_index, len(rows) - 1))
        return rows[self.thread_index]

    def toggle_browse(self) -> bool:
        """Show the whole repository in the file list, or only what the change touched."""
        self.browsing = not self.browsing
        if not self.browsing:
            self.viewing = None        # a repository file has nowhere to be listed any more
        self.file_index = 0
        self.scroll = self.body_cursor = 0
        return self.browsing

    def open_current(self) -> bool:
        """Open the selected row. A changed file is a diff; anything else is the file itself."""
        row = self.current
        if row is None:
            return False
        self.viewing = None if row.get("changed", True) else row["path"]
        self.scroll = self.body_cursor = 0
        return self.viewing is not None

    def cycle_thread_filter(self) -> str:
        self.thread_filter = "all" if self.thread_filter == "unresolved" else "unresolved"
        self.thread_index = 0
        return self.thread_filter

    def jump_to_thread(self) -> bool:
        """Open the file a discussion is anchored to and put the cursor on its line.

        A discussion about the whole change has nowhere to jump to, which is a fact about it rather
        than a failure — the caller says so instead of moving the cursor somewhere arbitrary.
        """
        thread = self.current_thread()
        anchor = (thread or {}).get("anchor") or {}
        path, line = anchor.get("file"), anchor.get("line")
        if not path or not line:
            return False
        for index, row in enumerate(self.files):
            if row["path"] == path:
                self.file_index = index
                break
        else:
            return False
        self.scroll = 0
        self.focus = "body"
        self.body_cursor = self._row_of_line(line)
        return True

    def _row_of_line(self, line: int) -> int:
        """The rendered row standing for a new-side line, or the top when none does.

        A discussion can be anchored to a line this mode does not render — an older version, or a
        line inside a gap nobody has unfolded — and landing at the top of the file is a better
        answer there than landing somewhere arbitrary.
        """
        for index, row in enumerate(self.body_rows()):
            if row.get("line") == line:
                return index
        return 0

    def draft_body(self) -> str:
        """The comment already prepared for whatever the rail points at, so editing one reopens it.

        Empty when there is none, which is also what the composer wants to start from.
        """
        anchor = self.subject()
        wanted = anchor["id"] if anchor else None
        for draft in self.review.get("drafts") or []:
            if draft.get("highlight_id") == wanted and draft.get("status") != "posted":
                return draft.get("body") or ""
        return ""

    def subject(self) -> dict | None:
        """What the chat pane is about: whatever the cursor is on, else the review.

        The cursor is the terminal's selection, so the conversation follows it the way the open
        file follows the file cursor — one place to point at a thing, and everything about that
        thing follows. A discussion is a subject like a highlight is, so pointing at one opens
        what has been said about it privately, beside what the merge request says publicly.
        """
        if self.focus == "threads":
            thread = self.current_thread()
            return {"kind": "thread", "id": thread["id"]} if thread else None
        rows = self.highlights
        if self.focus != "rail" or not rows:
            return None
        return {"kind": "highlight", "id": rows[max(0, min(self.rail_index, len(rows) - 1))]["id"]}

    def conversation_scope(self) -> str:
        anchor = self.subject()
        return (f"chat:{self.session}:review" if anchor is None
                else f"chat:{self.session}:{anchor['kind']}:{anchor['id']}")

    @property
    def conversation(self) -> dict:
        return self.client.views.get(self.conversation_scope()) or {}

    @property
    def highlights(self) -> list[dict]:
        """This session's highlights, newest last. The rail is session-wide; the overlay selects."""
        return self.rail.get("highlights", [])

    def highlights_here(self) -> list[dict]:
        row = self.current
        return [h for h in self.highlights if row and h["file"] == row["path"]]

    def marked_lines(self) -> set[int]:
        lines: set[int] = set()
        for highlight in self.highlights_here():
            lines.update(range(highlight["start"], highlight["end"] + 1))
        return lines

    def wanted(self) -> list[str]:
        scopes = [self.listing, f"rail:{self.session}", f"chat:{self.session}",
                  f"review:{self.session}", f"threads:{self.session}", f"access:{self.session}",
                  self.conversation_scope()]
        if self.browsing:
            scopes.append(f"tree:{self.session}")
        blob = self.blob_scope
        if blob:
            scopes.append(blob)       # a repository file is read from the blob, not from a diff
        body = self.body_scope
        if body:
            scopes.append(body)
        return scopes

    # --- rendering ---------------------------------------------------------

    def fragments(self) -> list[tuple[str, str]]:
        view = self.client.views.get(self.listing)
        out: list[tuple[str, str]] = []
        if view is None:
            return [("class:muted", " loading the change\u2026\n")]
        mr = view.get("mr") or {}
        title = f"{mr.get('project', '')}!{mr.get('iid', '')}  {mr.get('title', '')}"
        out.append(("class:header", f" {title}\n"))
        out.append(("class:muted", f"  mode {self.mode}   [{self.client.status}]"))
        out.extend(self._agent_badge())
        out.extend(self._review_badge())
        out.extend(self._pass_badge())
        out.extend(self._access_badge())
        if not view.get("head_aligned", True):
            out.append(("class:attention", "   read-only: the MR moved past this session"))
        if view.get("clean") is False:
            out.append(("class:attention", "   replay conflicted: may include target-branch changes"))
        out.append(("", "\n"))
        state = view.get("state", "ready")
        if state != "ready":
            note = {"loading": "resolving\u2026", "unavailable": "this host cannot serve that mode",
                    "error": view.get("error", ""), "unsupported-mode": "mode not supported",
                    "malformed-name": "bad scope name", "unknown-session": "this review is not open",
                    }.get(state, state)
            out.append(("class:error" if state == "error" else "class:muted", f"\n  {note}\n"))
            out.append(("class:footer", self._footer()))
            return out
        out.extend(self._file_pane())
        out.append(("", "\n"))
        out.extend(self._body_pane())
        out.extend(self._rail_pane())
        out.extend(self._thread_pane())
        out.extend(self._chat_pane())
        error = self.client.errors.get(f"rail:{self.session}") or self.client.last_command_error
        if error:
            out.append(("class:error", f"\n {error}\n"))
        out.append(("class:footer", self._footer()))
        return out

    def _file_pane(self) -> list[tuple[str, str]]:
        rows = self.browse_rows()
        if not rows:
            return [("class:muted", "  no files in this view\n")]
        out: list[tuple[str, str]] = []
        if self.browsing:
            state = self.tree.get("state", "idle")
            note = {"ready": f"{len(self.repo_paths())} files in the repository",
                    "loading": "reading the repository\u2026",
                    "unavailable": "this host cannot list the repository",
                    "error": self.tree.get("error", "")}.get(state, "reading the repository\u2026")
            out.append(("class:muted", f"  {note}\n"))
        top = max(0, min(self.file_index - FILE_PANE_ROWS // 2, len(rows) - FILE_PANE_ROWS))
        for index in range(top, min(top + FILE_PANE_ROWS, len(rows))):
            row = rows[index]
            selected = index == self.file_index
            marker = "\u203a" if selected else " "
            # a repository file has no counts to show — it is not part of the change
            counts = (f"+{row.get('additions', 0)} -{row.get('deletions', 0)}"
                      if row.get("changed") else "")
            style = "class:selected" if selected and self.focus == "files" else ""
            asked = sum(1 for h in self.highlights if h["file"] == row.get("path"))
            out.append(("class:muted", f" {marker} {counts:>9}  "))
            out.append((style if row.get("changed") else (style or "class:muted"),
                        f"{row.get('path', '')}"))
            out.append(("class:info", f"  {asked} asked\n" if asked else "\n"))
        if len(rows) > FILE_PANE_ROWS:
            out.append(("class:muted", f"   \u2026 {len(rows)} files\n"))
        return out

    def blob_rows(self) -> list[dict]:
        """A repository file as itself: numbered lines, coloured by the same token kinds.

        It is not a diff, so there are no sides and nothing to select — reading is all this offers,
        which is what browsing beyond the change is for.
        """
        view = self.blob
        state = view.get("state")
        if state != "ready":
            note = {"loading": "reading the file\u2026",
                    "unknown-file": "no such file at this version",
                    "unavailable": "this host cannot read files"}.get(state, view.get("error", ""))
            return [{"line": None, "pieces": [("class:muted", f"  {note or 'reading…'}\n")]}]
        rows: list[dict] = []
        for line in view.get("lines", []):
            pieces = line_fragments(line.get("text", ""), line.get("tokens") or [], "")
            rows.append({"line": None, "pieces": [("class:muted", f"{line['n']:>5}  ")] + pieces
                         + [("", "\n")]})
        return rows or [{"line": None, "pieces": [("class:muted", "  empty file\n")]}]

    def body_rows(self) -> list[dict]:
        """Every rendered body row, each carrying the new-side line it stands for (or None)."""
        if self.viewing:
            return self.blob_rows()
        scope = self.body_scope
        view = self.client.views.get(scope) if scope else None
        if view is None or view.get("state") != "ready":
            return []
        marked = self.marked_lines()
        rows: list[dict] = []
        for hunk in view.get("hunks", []):
            if hunk.get("gap_before"):
                rows.append({"line": None, "pieces": [
                    ("class:muted", f"  \u22ef {hunk['gap_before']} unchanged lines \u22ef\n")]})
            rows.append({"line": None, "pieces": [
                ("class:hunk", f"  @@ {hunk.get('heading', '')}\n")]})
            for line in hunk.get("lines", []):
                side = line.get("side", "context")
                base = SIDE_BASE.get(side, "")
                number = line.get("new")
                mark = "\u258c" if number in marked else " "
                gutter = f"{str(line.get('old') or ''):>5}{str(number or ''):>6} "
                pieces = [("class:info" if mark.strip() else "class:muted", mark),
                          ("class:muted", gutter),
                          (base, SIDE_MARK.get(side, " "))]
                pieces.extend(line_fragments(line.get("text", ""), line.get("tokens", []), base))
                pieces.append((base, "\n"))
                rows.append({"line": number, "pieces": pieces})
        return rows

    def _body_pane(self) -> list[tuple[str, str]]:
        # a repository file speaks for itself: `blob_rows` says what state it is in, so the
        # readiness checks below belong to the diff it is standing in place of
        if not self.viewing:
            scope = self.body_scope
            if scope is None:
                return []
            view = self.client.views.get(scope)
            if view is None:
                return [("class:muted", "  loading the file\u2026\n")]
            if view.get("state") != "ready":
                return [("class:muted", f"  {view.get('error') or view.get('state')}\n")]
        rows = self.body_rows()
        height = max(self.rows - FILE_PANE_ROWS - RAIL_PANE_ROWS - 8, 4)
        self.body_cursor = max(0, min(self.body_cursor, max(len(rows) - 1, 0)))
        if self.body_cursor < self.scroll:
            self.scroll = self.body_cursor
        elif self.body_cursor >= self.scroll + height:
            self.scroll = self.body_cursor - height + 1
        self.scroll = max(0, min(self.scroll, max(len(rows) - height, 0)))
        selecting = self.selected_range()
        out: list[tuple[str, str]] = []
        for index in range(self.scroll, min(self.scroll + height, len(rows))):
            row = rows[index]
            on_cursor = index == self.body_cursor and self.focus == "body"
            in_selection = selecting and row["line"] is not None and \
                selecting[0] <= row["line"] <= selecting[1]
            if on_cursor or in_selection:
                style = "class:selected" if on_cursor else "class:selecting"
                out.append((style, "".join(text for _, text in row["pieces"]).rstrip("\n")))
                out.append(("", "\n"))
            else:
                out.extend(row["pieces"])
        return out

    def _rail_pane(self) -> list[tuple[str, str]]:
        rows = self.highlights
        out: list[tuple[str, str]] = [("class:header", "\n Asked\n")]
        if not rows:
            out.append(("class:muted", "   nothing yet \u2014 v selects lines, v again asks\n"))
            return out
        self.rail_index = max(0, min(self.rail_index, len(rows) - 1))
        top = max(0, min(self.rail_index - RAIL_PANE_ROWS // 2, len(rows) - RAIL_PANE_ROWS))
        for index in range(top, min(top + RAIL_PANE_ROWS, len(rows))):
            highlight = rows[index]
            selected = index == self.rail_index and self.focus == "rail"
            card = highlight.get("card")
            context = highlight.get("context") or {}
            if card:
                answer = card["body"].splitlines()[0][:60]
                style = "class:ok"
            elif context.get("state") == "ready" and context.get("blame"):
                answer = f"last touched by {context['blame'][0].get('author', '?')}"
                style = "class:muted"
            elif context.get("state") == "loading":
                answer = "looking\u2026"
                style = "class:muted"
            else:
                answer = "not asked"
                style = "class:muted"
            where = f"{highlight['file'].split('/')[-1]}:{highlight['start']}-{highlight['end']}"
            out.append(("class:selected" if selected else "class:info",
                        f" #{highlight['n']:<3}"))
            out.append(("class:muted", f"{where:<22} "))
            out.append(COMMENT_MARK.get(highlight.get("comment_state"), ("class:muted", "  ")))
            out.append((style, answer + ("  (stale)" if highlight.get("stale") else "") + "\n"))
        return out

    def review_pass(self) -> dict:
        return self.rail.get("review_pass") or {}

    def _pass_badge(self) -> list[tuple[str, str]]:
        """What the MR-wide pass is doing. Stale says so rather than going quiet."""
        passed = self.review_pass()
        if not passed.get("requested"):
            return []
        if passed.get("stale"):
            return [("class:attention", "   that pass was about an earlier version")]
        return [("class:info", "   Claude is reviewing the change")]

    def _access_badge(self) -> list[tuple[str, str]]:
        """Consent is the one thing here that blocks the agent rather than the reviewer, so it says
        so in the header where nothing has to be open to see it."""
        waiting = len(self.pending_access())
        if not waiting:
            return []
        what = "repo" if waiting == 1 else "repos"
        return [("class:attention", f"   Claude is waiting on {waiting} {what}  (C to decide)")]

    def _review_badge(self) -> list[tuple[str, str]]:
        """What is waiting to be sent, and whether this has already been approved.

        Silent when there is nothing prepared and no approval to report — a header line that always
        says "0 pending" is a line the reviewer stops reading.
        """
        review = self.review
        if not review:
            return []
        pending, posted = review.get("pending", 0), review.get("posted", 0)
        approval = review.get("approval") or {}
        out: list[tuple[str, str]] = []
        if pending:
            out.append(("class:attention", f"   {pending} to send"))
        if posted:
            out.append(("class:ok", f"   {posted} sent"))
        if approval.get("you_approved"):
            out.append(("class:ok", "   \u2713 approved"))
        if (review.get("version") or {}).get("behind"):
            out.append(("class:attention", "   moved since you read it"))
        return out

    def _thread_pane(self) -> list[tuple[str, str]]:
        view = self.threads
        if not view:
            return []
        rows = self.thread_rows()
        shown = "open" if self.thread_filter == "unresolved" else "all"
        head = f"\n Discussions · {view.get('unresolved', 0)} open of {view.get('total', 0)}"
        out: list[tuple[str, str]] = [("class:header", f"{head}  [{shown}]\n")]
        if not rows:
            out.append(("class:muted", "   nothing here — f shows all\n"))
            return out
        self.thread_index = max(0, min(self.thread_index, len(rows) - 1))
        top = max(0, min(self.thread_index - THREAD_PANE_ROWS // 2, len(rows) - THREAD_PANE_ROWS))
        for index in range(top, min(top + THREAD_PANE_ROWS, len(rows))):
            thread = rows[index]
            selected = index == self.thread_index and self.focus == "threads"
            anchor = thread.get("anchor") or {}
            where = (f"{anchor['file'].split('/')[-1]}:{anchor.get('line', '')}"
                     if anchor.get("file") else "whole MR")
            comments = thread.get("comments") or []
            said = _one_line(comments[0]["body"]) if comments else ""
            out.append(("class:selected" if selected else "class:info",
                        f" {'✓' if thread.get('resolved') else '●'} "))
            out.append(("class:muted", f"{where:<22} "))
            out.append(("class:muted" if thread.get("resolved") else "",
                        f"{said}  ({len(comments)})\n"))
        return out

    def _agent_badge(self) -> list[tuple[str, str]]:
        """Working, stalled, watching or off — the server's word, not a rule applied here."""
        agent = self.chat.get("agent") or {}
        state = agent.get("state")
        if not state:
            return []
        label = AGENT_LABEL.get(state, state)
        if agent.get("stale"):
            label += " (no answer yet)"
        return [("class:muted", "   "), (AGENT_STYLE.get(state, "class:muted"), label)]

    def _chat_pane(self) -> list[tuple[str, str]]:
        anchor = self.subject()
        if anchor is None:
            heading = "Chat \u2014 this review"
        else:
            row = next((h for h in self.highlights if h["id"] == anchor["id"]), None)
            where = f"#{row['n']} {row['file'].split('/')[-1]}:{row['start']}" if row else "a highlight"
            heading = f"Chat \u2014 {where}"
        view = self.conversation
        out: list[tuple[str, str]] = [("class:header", f"\n {heading}\n")]
        messages = view.get("messages") or []
        if not messages:
            out.append(("class:muted", "   nothing said yet \u2014 c writes a message\n"))
            return out
        for message in messages[-CHAT_PANE_ROWS:]:
            who = "you" if message["role"] == "user" else "claude"
            style = "class:info" if message["role"] == "user" else "class:ok"
            out.append((style, f"   {who:<7}"))
            out.append(("", _one_line(message["body"]) + "\n"))
        if view.get("checking"):
            out.append(("class:attention", "   Claude is double-checking this\n"))
        elif view.get("owed"):
            out.append(("class:muted", "   waiting on Claude\n"))
        return out

    def _footer(self) -> str:
        if self.anchor is not None:
            return "\n j/k extend   v ask about the selection   esc cancel\n"
        if self.focus == "body":
            return "\n tab pane   j/k line   v select   n/p file   m mode   b back   q quit\n"
        if self.focus == "rail":
            return ("\n tab pane   j/k move   a ask Claude   D double-check   c write   d comment"
                    "   S send   b back   q quit\n")
        if self.focus == "threads":
            return ("\n tab pane   j/k move   enter go to it   f open/all   c ask Claude"
                    "   D double-check   R reply   V resolve   b back   q quit\n")
        return ("\n tab pane   j/k move   i review pass   o browse repo   c write   d comment"
                "   S send   b back   q quit\n")

    # --- interaction ---------------------------------------------------------

    def selected_range(self) -> tuple[int, int] | None:
        if self.anchor is None:
            return None
        here = self.cursor_line()
        if here is None:
            return (self.anchor, self.anchor)
        return (min(self.anchor, here), max(self.anchor, here))

    def cursor_line(self) -> int | None:
        rows = self.body_rows()
        if not rows:
            return None
        index = max(0, min(self.body_cursor, len(rows) - 1))
        return rows[index]["line"]

    def move(self, delta: int) -> None:
        if self.focus == "files":
            rows = self.browse_rows()
            if rows:
                self.file_index = max(0, min(self.file_index + delta, len(rows) - 1))
                self.scroll = self.body_cursor = 0
        elif self.focus == "rail":
            rows = self.highlights
            if rows:
                self.rail_index = max(0, min(self.rail_index + delta, len(rows) - 1))
        elif self.focus == "threads":
            rows = self.thread_rows()
            if rows:
                self.thread_index = max(0, min(self.thread_index + delta, len(rows) - 1))
        else:
            rows = self.body_rows()
            self.body_cursor = max(0, min(self.body_cursor + delta, max(len(rows) - 1, 0)))

    def next_file(self, delta: int) -> None:
        rows = self.browse_rows()
        if rows:
            self.file_index = max(0, min(self.file_index + delta, len(rows) - 1))
            self.scroll = self.body_cursor = 0
            self.anchor = None

    def toggle_focus(self) -> None:
        order = ("files", "body", "rail", "threads")
        self.focus = order[(order.index(self.focus) + 1) % len(order)]

    def cycle_mode(self) -> str:
        """Switching mode is a subscription, not a command \u2014 the name changes and the server
        answers for the new one."""
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)] if self.mode in MODES else "full"
        self.scroll = self.body_cursor = 0
        self.anchor = None
        return self.mode

    def start_or_commit_selection(self) -> dict | None:
        """First press anchors, second returns the add_highlight command for the range."""
        here = self.cursor_line()
        if here is None:
            return None
        if self.anchor is None:
            self.anchor = here
            return None
        low, high = self.selected_range()
        self.anchor = None
        return {"type": "add_highlight", "file": self.current["path"], "side": "new",
                "line_range": {"start": low, "end": high}}

    def check_command(self) -> dict | None:
        """Ask Claude to verify what has been claimed about whatever the cursor is on.

        The subject is the one the chat pane is already about, so the doubt lands where the answer
        will be read. Nothing is offered for the review as a whole: a doubt has to be about a
        claim, and "the whole change" is not one — that is what a review pass is for.
        """
        subject = self.subject()
        if subject is None:
            return None
        return {"type": "request_check", "subject": subject}

    def cancel_selection(self) -> None:
        self.anchor = None

    def ask_command(self) -> dict | None:
        """Escalate the highlight in focus from the cheap tier to the agent."""
        rows = self.highlights
        if self.focus == "rail" and rows:
            target = rows[max(0, min(self.rail_index, len(rows) - 1))]
        else:
            line = self.cursor_line()
            target = next((h for h in self.highlights_here()
                           if line is not None and h["start"] <= line <= h["end"]), None)
        if target is None:
            return None
        return {"type": "request_context", "highlight_id": target["id"]}
