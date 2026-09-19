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
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.styles import Style

from review_mate.tui.client import ViewClient

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

    def bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("q")
        @kb.add("c-c")
        def _quit(event) -> None:
            event.app.exit()

        @kb.add("j")
        @kb.add("down")
        def _down(event) -> None:
            self.cursor = min(self.cursor + 1, max(len(self.rows()) - 1, 0))

        @kb.add("k")
        @kb.add("up")
        def _up(event) -> None:
            self.cursor = max(self.cursor - 1, 0)

        @kb.add("enter")
        def _track(event) -> None:
            row = self.selected()
            if row is None or row.kind != "queue":
                return
            ref = {"host": row.data.get("host"), "project": row.data.get("project"),
                   "iid": row.data.get("iid")}
            asyncio.create_task(self._run(self.client.command("session.open", ref=ref)))

        @kb.add("c")
        def _close(event) -> None:
            row = self.selected()
            if row is None or row.kind != "session":
                return
            asyncio.create_task(self._run(self.client.command("session.close",
                                                              id=row.data.get("id"))))

        @kb.add("r")
        def _refresh(event) -> None:
            asyncio.create_task(self._run(self.client.command("hub.refresh")))

        return kb

    async def _run(self, coro) -> None:
        await coro
        self.invalidate()

    def invalidate(self) -> None:
        app = getattr(self, "_app", None)
        if app is not None:
            app.invalidate()

    def build(self) -> Application:
        control = FormattedTextControl(self.fragments, focusable=True, show_cursor=False)
        layout = Layout(HSplit([Window(control, wrap_lines=False)]))
        app = Application(layout=layout, key_bindings=self.bindings(), style=STYLE,
                          full_screen=True, mouse_support=False)
        self._app = app
        return app
