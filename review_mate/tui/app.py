"""The hub screen: a terminal renderer for the `hub` scope.

Every value on screen comes from the view document — this module decides layout and key bindings
and nothing else. There is no review state here to fall out of step with the server's, which is
the property the second client exists to prove.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import ConditionalKeyBindings, KeyBindings, merge_key_bindings

from review_mate.session.state import Criticality, Theme
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit
from prompt_toolkit.styles import Style

from review_mate.tui.client import ViewClient
from review_mate.tui.diff import DiffScreen

# the vocabulary, read off the model so the prompt and the command cannot disagree
THEMES = [t.value for t in Theme]
CRITICALITIES = [c.value for c in Criticality]

STATE_BADGE = {
    "merged": ("merged", "class:merged"),
    "closed": ("closed", "class:muted"),
    "in_progress": ("drafts", "class:attention"),
    "git_update": ("updated", "class:attention"),
    "discussions": ("threads", "class:info"),
    "reviewed": ("reviewed", "class:ok"),
    "new": ("new", "class:muted"),
}

STYLE = Style.from_dict({
    "header": "bold",
    "selected": "reverse",
    "muted": "#808080",
    "ok": "#3fa34d",
    "info": "#3b82c4",
    "attention": "#d08400",
    "merged": "#8b5cf6",
    "error": "#c04040",
    "footer": "#808080",
    "hunk": "#5c6370",
    "selecting": "bg:#2b3a4a",
})


@dataclass
class Row:
    kind: str          # "session" | "queue"
    data: dict[str, Any]


class HubScreen:
    def __init__(self, client: ViewClient) -> None:
        self.client = client
        self.cursor = 0

    # --- reading the view -------------------------------------------------

    @property
    def view(self) -> dict[str, Any]:
        return self.client.views.get("hub", {})

    def rows(self) -> list[Row]:
        view = self.view
        rows = [Row("session", s) for s in view.get("sessions", [])]
        open_pairs = {((s.get("mr") or {}).get("project"), (s.get("mr") or {}).get("iid"))
                      for s in view.get("sessions", [])}
        for item in view.get("queue", []):
            if (item.get("project"), item.get("iid")) in open_pairs:
                continue          # already an open review — it is listed above, not twice
            rows.append(Row("queue", item))
        return rows

    def selected(self) -> Row | None:
        rows = self.rows()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        return rows[self.cursor]

    # --- rendering --------------------------------------------------------

    def fragments(self) -> list[tuple[str, str]]:
        view = self.view
        rows = self.rows()
        out: list[tuple[str, str]] = []

        user = view.get("user") or "—"
        checked = view.get("host_checked_at") or "not checked"
        out.append(("class:header", f" review-mate · {user}"))
        out.append(("class:muted", f"   [{self.client.status}]   host: {checked}\n\n"))

        sessions = [r for r in rows if r.kind == "session"]
        out.append(("class:header", " Open reviews\n"))
        if not sessions:
            out.append(("class:muted", "   none\n"))
        for row in rows:
            if row.kind != "session":
                continue
            out.extend(self._session_line(row, rows.index(row)))

        out.append(("", "\n"))
        queue_state = view.get("queue_state", "idle")
        label = {"loading": "loading…", "error": "unavailable", "ready": "",
                 "idle": ""}.get(queue_state, "")
        out.append(("class:header", " Review queue"))
        out.append(("class:muted", f"  {label}\n"))
        if queue_state == "error":
            out.append(("class:error", f"   {view.get('queue_error', '')}\n"))
        queued = [r for r in rows if r.kind == "queue"]
        if not queued and queue_state == "ready":
            out.append(("class:muted", "   nothing waiting on you\n"))
        for row in rows:
            if row.kind != "queue":
                continue
            out.extend(self._queue_line(row, rows.index(row)))

        error = self.client.errors.get("hub") or self.client.last_command_error
        if error:
            out.append(("class:error", f"\n {error}\n"))
        out.append(("class:footer",
                    "\n j/k move   enter track   c close   r refresh   q quit\n"))
        return out

    def _cursor_style(self, index: int) -> str:
        return "class:selected" if index == self.cursor else ""

    def _session_line(self, row: Row, index: int) -> list[tuple[str, str]]:
        data = row.data
        mr = data.get("mr") or {}
        badge, badge_style = STATE_BADGE.get(data.get("state", "new"), ("?", "class:muted"))
        checked = "" if data.get("host_checked") else "?"
        marker = "›" if index == self.cursor else " "
        head = f" {marker} {badge:>9}{checked:<1} "
        title = f"{mr.get('label') or '(no MR)'}  {mr.get('title', '')}"
        counts = []
        if data.get("pending"):
            counts.append(f"{data['pending']} draft")
        if data.get("unresolved"):
            counts.append(f"{data['unresolved']} unresolved")
        tail = f"   {', '.join(counts)}" if counts else ""
        return [(badge_style, head),
                (self._cursor_style(index), title),
                ("class:muted", tail + "\n")]

    def _queue_line(self, row: Row, index: int) -> list[tuple[str, str]]:
        data = row.data
        marker = "›" if index == self.cursor else " "
        head = f" {marker} {'':>10} "
        title = f"{data.get('project', '')}!{data.get('iid', '')}  {data.get('title', '')}"
        return [("class:muted", head), (self._cursor_style(index), title + "\n")]

    # --- interaction ------------------------------------------------------

    def selected_session(self) -> str | None:
        row = self.selected()
        return row.data.get("id") if row is not None and row.kind == "session" else None

    def selected_ref(self) -> dict | None:
        row = self.selected()
        if row is None or row.kind != "queue":
            return None
        return {"host": row.data.get("host"), "project": row.data.get("project"),
                "iid": row.data.get("iid")}

    def move(self, delta: int) -> None:
        self.cursor = max(0, min(self.cursor + delta, max(len(self.rows()) - 1, 0)))


class Shell:
    """Holds the screens and the subscription set.

    Navigating is subscribing: opening a review watches its scopes and leaving drops them, so the
    server only builds what someone is looking at. The shell owns that because it is the only part
    that knows which screen is in front.
    """

    def __init__(self, client: ViewClient) -> None:
        self.client = client
        self.hub = HubScreen(client)
        self.diff: DiffScreen | None = None
        self._app: Application | None = None
        # writing a message takes the keyboard: the buffer is focused, so navigation keys are
        # text while it is open rather than every binding needing to know about composing
        self.composing = False
        # One composer, two kinds. A message is a line and sends on enter; a review comment is
        # prose the reviewer shapes, so enter breaks the line there and c-s saves it.
        self.compose_kind = "message"
        # sending a review reaches the merge request, so it asks first and the answer is a keypress
        self.approve = False
        self.confirming = False
        # the consent prompt is modal: it blocks the agent, and answering it by accident while
        # navigating would grant a repository read
        self.deciding: dict | None = None
        # the insight being relabelled, and which half is being chosen
        self.labelling: dict | None = None
        self.compose = Buffer(multiline=Condition(lambda: self.compose_kind == "draft"))
        self._compose_window: Window | None = None
        self._main_window: Window | None = None

    @property
    def screen(self):
        return self.diff or self.hub

    def fragments(self) -> list[tuple[str, Any]]:
        screen = self.screen
        if isinstance(screen, DiffScreen):
            screen.rows = self._rows()
            return (screen.fragments() + self._deciding_line() + self._labelling_line()
                    + self._sending_line())
        return screen.fragments()

    def _deciding_line(self) -> list[tuple[str, Any]]:
        """What is being granted, spelled out. A repository read is not undone by reopening it."""
        request = self.deciding
        if request is None:
            return []
        return [("class:attention",
                 f" let Claude read {request['repo']}?  {request.get('reason', '')}\n"
                 f" y to allow, n to refuse, esc to leave it waiting\n")]

    def _labelling_line(self) -> list[tuple[str, Any]]:
        """Disagreeing with how Claude classified a finding, one half at a time.

        Theme first because it is the harder of the two to change your mind about; criticality is
        then a three-way choice. Escape leaves the label exactly as it was — abandoning halfway
        must not write half a label.
        """
        if self.labelling is None:
            return []
        if self.labelling.get("theme") is None:
            options = "  ".join(f"{i + 1} {name}" for i, name in enumerate(THEMES))
            return [("class:attention", f" what is it about?  {options}   esc cancel\n")]
        options = "  ".join(f"{i + 1} {name}" for i, name in enumerate(CRITICALITIES))
        return [("class:attention",
                 f" {self.labelling['theme']} — how much does it matter?  {options}"
                 f"   esc cancel\n")]

    def _sending_line(self) -> list[tuple[str, Any]]:
        """What pressing S is about to do, in the reviewer's words, before it happens.

        Sending reaches the merge request and cannot be taken back, so the count and the approval
        are spelled out rather than left to whatever the reviewer remembers arming.
        """
        if self.diff is None:
            return []
        pending = (self.diff.review.get("pending") or 0)
        if self.confirming:
            what = f"send {pending} comment{'' if pending == 1 else 's'}" if pending else "approve"
            if pending and self.approve:
                what += " and approve"
            return [("class:attention", f" {what}?  y to confirm, esc to cancel\n")]
        if self.approve:
            return [("class:info", " approval armed \u2014 S sends it with your comments\n")]
        return []

    def _rows(self) -> int:
        app = self._app
        try:
            return app.output.get_size().rows if app is not None else 24
        except Exception:
            return 24

    def invalidate(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    def on_change(self) -> None:
        """Every view update: repaint, and pick up any scope the screen can only ask for now.

        A review screen cannot name the file it wants until the file list has arrived, so the
        subscription set is reconciled whenever a view lands rather than only when the reader
        navigates.
        """
        self.invalidate()
        if self.diff is None:
            return
        missing = [scope for scope in self.diff.wanted() if scope not in self.client.scopes]
        if missing:
            asyncio.create_task(self.run_command(self.client.watch(missing)))

    # --- navigation ---------------------------------------------------------

    async def open_review(self, session: str) -> None:
        self.diff = DiffScreen(self.client, session)
        await self.client.watch(self.diff.wanted())
        self.invalidate()

    async def leave_review(self) -> None:
        if self.diff is None:
            return
        await self.client.unwatch(self.diff.wanted())
        self.diff = None
        self.invalidate()

    async def resync(self, previous: list[str]) -> None:
        """Bring the subscription set in line with what the open screen now needs."""
        if self.diff is None:
            return
        wanted = self.diff.wanted()
        stale = [scope for scope in previous if scope not in wanted]
        if stale:
            await self.client.unwatch(stale)
        await self.client.watch(wanted)
        self.invalidate()

    # --- writing a message ---------------------------------------------------

    def start_compose(self, kind: str = "message") -> None:
        if self.diff is None:
            return
        self.composing = True
        self.compose_kind = kind
        self.compose.reset()
        if kind == "draft":
            # editing rather than writing from scratch: a comment already prepared for this
            # subject comes back, so saving again is a correction and not a second comment
            self.compose.text = self.diff.draft_body()
        if self._app is not None and self._compose_window is not None:
            self._app.layout.focus(self._compose_window)
        self.invalidate()

    def cancel_compose(self) -> None:
        self.composing = False
        self.compose_kind = "message"
        # sending a review reaches the merge request, so it asks first and the answer is a keypress
        self.approve = False
        self.confirming = False
        # the consent prompt is modal: it blocks the agent, and answering it by accident while
        # navigating would grant a repository read
        self.deciding: dict | None = None
        self.compose.reset()
        if self._app is not None and self._main_window is not None:
            self._app.layout.focus(self._main_window)
        self.invalidate()

    def compose_submission(self) -> tuple[str, dict] | None:
        """How to send what is being written: which call to make, and what to give it.

        The kinds do not share a path, and the seam names the call rather than assuming one. A
        message and a comment change this session, so they are session commands. A reply changes
        the merge request, so it is a view command about a discussion.
        """
        body = self.compose.text.strip()
        if not body or self.diff is None:
            return None
        anchor = self.diff.subject()
        if self.compose_kind == "reply":
            thread = self.diff.current_thread()
            if thread is None:
                return None
            return "view", {"cmd": "thread.reply", "session": self.diff.session,
                            "thread": thread["id"], "body": body}
        if self.compose_kind == "draft":
            # a comment anchors to a line, so a cursor sitting on a discussion writes the summary
            # rather than hanging a comment off a thread id
            on_line = anchor["id"] if anchor and anchor["kind"] == "highlight" else None
            return "session", {"type": "save_draft", "highlight_id": on_line, "body": body}
        command: dict = {"type": "post_message", "body": body}
        if anchor is not None:
            command["anchor"] = anchor
        return "session", command

    def compose_prompt(self) -> list[tuple[str, str]]:
        """Who reads what is being typed, in one word, before it is sent."""
        if self.diff is None:
            return [("class:muted", " ")]
        anchor = self.diff.subject()
        if self.compose_kind == "reply":
            return [("class:attention", " reply> ")]      # everyone on the merge request
        if self.compose_kind == "draft":
            return [("class:info", " note> " if anchor is None else " comment> ")]
        return [("class:info", " say> " if anchor is None else " ask> ")]

    async def run_command(self, coroutine) -> None:
        await coroutine
        self.invalidate()

    # --- keys ----------------------------------------------------------------

    def bindings(self) -> KeyBindings:
        kb = KeyBindings()

        def spawn(coroutine):
            asyncio.create_task(self.run_command(coroutine))

        @kb.add("q")
        @kb.add("c-c")
        def _quit(event) -> None:
            event.app.exit()

        def _opened() -> None:
            """Reading a repository file is a subscription change — it is read from its blob."""
            if self.diff is None:
                return
            previous = self.diff.wanted()
            self.diff.open_current()
            spawn(self.resync(previous))

        def _moved(delta: int) -> None:
            # the rail cursor picks the subject, so moving it changes which conversation is watched
            previous = self.diff.wanted() if self.diff is not None else []
            self.screen.move(delta)
            if self.diff is None:
                return
            # the rail and the discussions pick a subject, so moving either changes which
            # conversation is watched; the file list picks what the body reads
            if self.diff.focus in ("rail", "threads") or (self.diff.focus == "files"
                                                          and self.diff.browsing):
                if self.diff.focus == "files":
                    self.diff.open_current()
                spawn(self.resync(previous))

        @kb.add("j")
        @kb.add("down")
        def _down(event) -> None:
            _moved(1)

        @kb.add("k")
        @kb.add("up")
        def _up(event) -> None:
            _moved(-1)

        @kb.add("enter")
        def _enter(event) -> None:
            if self.diff is not None:
                # on a discussion, enter means go to what it is about; elsewhere it moves on
                if self.diff.focus == "threads" and self.diff.jump_to_thread():
                    self.invalidate()
                    return
                self.diff.toggle_focus()
                return
            session = self.hub.selected_session()
            if session:
                spawn(self.open_review(session))
                return
            ref = self.hub.selected_ref()
            if ref:
                spawn(self.client.command("session.open", ref=ref))

        @kb.add("tab")
        def _tab(event) -> None:
            if self.diff is not None:
                self.diff.toggle_focus()

        @kb.add("v")
        def _select(event) -> None:
            """First press anchors a range, second asks about it."""
            if self.diff is None:
                return
            command = self.diff.start_or_commit_selection()
            if command is not None:
                spawn(self.client.session_command(self.diff.session, command))
            self.invalidate()

        @kb.add("escape", eager=True)
        def _cancel(event) -> None:
            if self.confirming:
                self.confirming = False
                self.invalidate()
                return
            if self.diff is not None and self.diff.anchor is not None:
                self.diff.cancel_selection()
                self.invalidate()
                return
            if self.diff is not None:
                spawn(self.leave_review())

        @kb.add("a")
        def _ask(event) -> None:
            if self.diff is None:
                return
            command = self.diff.ask_command()
            if command is None:
                return
            spawn(self.client.session_command(self.diff.session, command))

        @kb.add("D")
        def _double_check(event) -> None:
            """Doubt what has been said about this — yours or Claude's — and ask it to verify."""
            if self.diff is None:
                return
            command = self.diff.check_command()
            if command is None:
                return
            spawn(self.client.session_command(self.diff.session, command))

        @kb.add("L")
        def _relabel(event) -> None:
            """Disagree with how Claude classified the finding under the cursor."""
            row = self.diff.rail_row() if self.diff is not None else None
            if row is None or row["kind"] != "insight" or self.diff.focus != "rail":
                return
            self.labelling = {"card_id": row["data"]["id"], "theme": None}
            self.invalidate()

        @kb.add("n")
        def _next_file(event) -> None:
            if self.diff is not None:
                previous = self.diff.wanted()
                self.diff.next_file(1)
                spawn(self.resync(previous))

        @kb.add("p")
        def _prev_file(event) -> None:
            if self.diff is not None:
                previous = self.diff.wanted()
                self.diff.next_file(-1)
                spawn(self.resync(previous))

        @kb.add("m")
        def _mode(event) -> None:
            if self.diff is not None:
                previous = self.diff.wanted()
                self.diff.cycle_mode()
                spawn(self.resync(previous))

        @kb.add("b")
        def _back(event) -> None:
            if self.diff is not None:
                spawn(self.leave_review())

        @kb.add("c")
        def _close(event) -> None:
            if self.diff is not None:
                self.start_compose()
                return
            session = self.hub.selected_session()
            if session:
                spawn(self.client.command("session.close", id=session))

        @kb.add("r")
        def _refresh(event) -> None:
            if self.diff is None:
                spawn(self.client.command("hub.refresh"))

        @kb.add("R")
        def _reply(event) -> None:
            """Answer the discussion under the cursor. Everyone on the merge request reads it."""
            if self.diff is not None and self.diff.current_thread() is not None:
                self.start_compose("reply")

        @kb.add("V")
        def _resolve(event) -> None:
            """Settle a discussion, or reopen one. Reversible, so it goes without asking."""
            thread = self.diff.current_thread() if self.diff is not None else None
            if thread is None:
                return
            spawn(self.client.command("thread.resolve", session=self.diff.session,
                                      thread=thread["id"], resolved=not thread.get("resolved")))

        @kb.add("i")
        def _review_pass(event) -> None:
            """Ask Claude to review the change as a whole — alongside your own pass, not instead."""
            if self.diff is None or not self.diff.review_pass().get("available", True):
                return
            spawn(self.client.session_command(self.diff.session, {"type": "request_insights"}))

        @kb.add("o")
        def _browse(event) -> None:
            """Show the whole repository in the file list, or only the change."""
            if self.diff is None:
                return
            previous = self.diff.wanted()
            self.diff.toggle_browse()
            spawn(self.resync(previous))

        @kb.add("C")
        def _consent(event) -> None:
            """Decide what Claude has asked to read. Nothing is read until this is answered."""
            waiting = self.diff.pending_access() if self.diff is not None else []
            if waiting:
                self.deciding = waiting[0]
                self.invalidate()

        @kb.add("f")
        def _filter(event) -> None:
            """Open discussions, or all of them — the filter a reviewer reaches for first."""
            if self.diff is not None:
                self.diff.cycle_thread_filter()
                self.invalidate()

        @kb.add("d")
        def _draft(event) -> None:
            """Write the review comment for whatever the rail points at, or for the MR itself."""
            if self.diff is not None:
                self.start_compose("draft")

        @kb.add("A")
        def _approve(event) -> None:
            """Arm the approval. It travels with the next submission rather than on its own, so
            approving and commenting are one decision the reviewer makes once."""
            if self.diff is not None and (self.diff.review.get("approval") or {}).get("available"):
                self.approve = not self.approve
                self.invalidate()

        @kb.add("S")
        def _submit(event) -> None:
            if self.diff is None:
                return
            if not (self.diff.review.get("pending") or self.approve):
                return                      # nothing to send and nothing to approve
            self.confirming = True
            self.invalidate()

        @kb.add("y")
        def _confirm(event) -> None:
            if not self.confirming or self.diff is None:
                return
            self.confirming = False
            spawn(self.client.command("review.submit", session=self.diff.session,
                                      approve=self.approve))
            self.approve = False

        @kb.add("x")
        def _discard(event) -> None:
            if self.diff is None or not self.diff.draft_body():
                return
            anchor = self.diff.subject()
            spawn(self.client.session_command(
                self.diff.session,
                {"type": "remove_draft", "highlight_id": anchor["id"] if anchor else None}))

        deciding_kb = KeyBindings()      # a consent prompt, which takes the keyboard while open

        def _decide(approve: bool) -> None:
            request, session = self.deciding, self.diff.session if self.diff else None
            self.deciding = None
            if request is not None and session is not None:
                spawn(self.client.session_command(
                    session, {"type": "decide_access", "request_id": request["id"],
                              "approve": approve}))
            self.invalidate()

        @deciding_kb.add("y")
        def _grant(event) -> None:
            _decide(True)

        @deciding_kb.add("n")
        def _refuse(event) -> None:
            _decide(False)

        @deciding_kb.add("escape", eager=True)
        def _leave(event) -> None:
            self.deciding = None
            self.invalidate()

        labelling_kb = KeyBindings()     # a relabel prompt, which takes the keyboard while open

        def _choose(index: int) -> None:
            if self.labelling is None:
                return
            if self.labelling["theme"] is None:
                if index < len(THEMES):
                    self.labelling["theme"] = THEMES[index]
                self.invalidate()
                return
            if index >= len(CRITICALITIES):
                return
            pending, session = self.labelling, self.diff.session if self.diff else None
            self.labelling = None
            if session is not None:
                spawn(self.client.session_command(session, {
                    "type": "label_card", "card_id": pending["card_id"],
                    "label": {"theme": pending["theme"],
                              "criticality": CRITICALITIES[index], "about": ""}}))
            self.invalidate()

        for _slot in range(max(len(THEMES), len(CRITICALITIES))):
            labelling_kb.add(str(_slot + 1))(
                lambda event, index=_slot: _choose(index))

        @labelling_kb.add("escape", eager=True)
        def _abandon_label(event) -> None:
            self.labelling = None       # halfway out must not write half a label
            self.invalidate()

        writing = KeyBindings()          # whichever kind is open
        sending = KeyBindings()          # a message is one line, so enter is its whole gesture

        def _commit() -> None:
            submission = self.compose_submission()
            session = self.diff.session if self.diff is not None else None
            self.cancel_compose()
            if submission is None or session is None:
                return
            via, payload = submission
            if via == "view":
                spawn(self.client.command(payload.pop("cmd"), **payload))
            else:
                spawn(self.client.session_command(session, payload))

        @sending.add("enter")
        def _send(event) -> None:
            _commit()

        @writing.add("c-s")
        def _save(event) -> None:
            _commit()

        @writing.add("escape", eager=True)
        def _abandon(event) -> None:
            self.cancel_compose()

        composing = Condition(lambda: self.composing)
        deciding = Condition(lambda: self.deciding is not None)
        labelling = Condition(lambda: self.labelling is not None)
        one_line = Condition(lambda: self.compose_kind == "message")
        # enter belongs to the buffer while a comment is being written — that is what makes it
        # prose rather than a line — so only the one-line kind binds it
        return merge_key_bindings([
            ConditionalKeyBindings(kb, ~composing & ~deciding & ~labelling),
            ConditionalKeyBindings(sending, composing & one_line),
            ConditionalKeyBindings(writing, composing),
            ConditionalKeyBindings(deciding_kb, deciding),
            ConditionalKeyBindings(labelling_kb, labelling)])

    def build(self) -> Application:
        control = FormattedTextControl(self.fragments, focusable=True, show_cursor=False)
        self._main_window = Window(control, wrap_lines=False)
        self._compose_window = Window(BufferControl(self.compose), height=1)
        prompt = Window(FormattedTextControl(self.compose_prompt), height=1, width=8)
        layout = Layout(HSplit([
            self._main_window,
            ConditionalContainer(VSplit([prompt, self._compose_window]),
                                 filter=Condition(lambda: self.composing)),
        ]))
        self._app = Application(layout=layout, key_bindings=self.bindings(), style=STYLE,
                                full_screen=True, mouse_support=False)
        return self._app
