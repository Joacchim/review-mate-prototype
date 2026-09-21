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
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit
from prompt_toolkit.styles import Style

from review_mate.tui.client import ViewClient
from review_mate.tui.diff import DiffScreen

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
        title = f"{mr.get('project', '(no MR)')}!{mr.get('iid', '')}  {mr.get('title', '')}"
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
        self.compose = Buffer(multiline=False)
        self._compose_window: Window | None = None
        self._main_window: Window | None = None

    @property
    def screen(self):
        return self.diff or self.hub

    def fragments(self) -> list[tuple[str, Any]]:
        screen = self.screen
        if isinstance(screen, DiffScreen):
            screen.rows = self._rows()
        return screen.fragments()

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

    def start_compose(self) -> None:
        if self.diff is None:
            return
        self.composing = True
        self.compose.reset()
        if self._app is not None and self._compose_window is not None:
            self._app.layout.focus(self._compose_window)
        self.invalidate()

    def cancel_compose(self) -> None:
        self.composing = False
        self.compose.reset()
        if self._app is not None and self._main_window is not None:
            self._app.layout.focus(self._main_window)
        self.invalidate()

    def compose_command(self) -> dict | None:
        """The message being written, as a command — anchored to whatever the screen is about."""
        body = self.compose.text.strip()
        if not body or self.diff is None:
            return None
        command: dict = {"type": "post_message", "body": body}
        anchor = self.diff.subject()
        if anchor is not None:
            command["anchor"] = anchor
        return command

    def compose_prompt(self) -> list[tuple[str, str]]:
        if self.diff is None:
            return [("class:muted", " ")]
        anchor = self.diff.subject()
        return [("class:info", " say> " if anchor is None else " reply> ")]

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

        def _moved(delta: int) -> None:
            # the rail cursor picks the subject, so moving it changes which conversation is watched
            previous = self.diff.wanted() if self.diff is not None else []
            self.screen.move(delta)
            if self.diff is not None and self.diff.focus == "rail":
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

        writing = KeyBindings()

        @writing.add("enter")
        def _send(event) -> None:
            command = self.compose_command()
            session = self.diff.session if self.diff is not None else None
            self.cancel_compose()
            if command is not None and session is not None:
                spawn(self.client.session_command(session, command))

        @writing.add("escape", eager=True)
        def _abandon(event) -> None:
            self.cancel_compose()

        composing = Condition(lambda: self.composing)
        return merge_key_bindings([ConditionalKeyBindings(kb, ~composing),
                                   ConditionalKeyBindings(writing, composing)])

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
