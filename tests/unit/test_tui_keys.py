"""The key bindings: that a keypress reaches the action it is supposed to.

The screens are tested directly elsewhere, and the protocol end to end in the integration suite.
What neither covers is the wiring between them — a binding pointing at the wrong method, or a key
that silently does nothing. Driving a terminal to find that out is unreliable to script; calling
the handler the binding registered is not.
"""
import asyncio

import pytest

pytest.importorskip("prompt_toolkit")

from prompt_toolkit.key_binding.key_bindings import _parse_key   # noqa: E402

from review_mate.tui.app import Shell                            # noqa: E402
from review_mate.tui.diff import DiffScreen                      # noqa: E402


class RecordingClient:
    def __init__(self, views):
        self.views = views
        self.status = "live"
        self.errors = {}
        self.last_command_error = ""
        self.session_commands = []
        self.commands = []
        self.watched = []

    async def session_command(self, session, command):
        self.session_commands.append((session, command))
        return True

    async def command(self, cmd, **args):
        self.commands.append((cmd, args))
        return True

    async def watch(self, scopes):
        self.watched.append(list(scopes))

    async def unwatch(self, scopes):
        return None

    @property
    def scopes(self):
        return [s for group in self.watched for s in group]


def views():
    def line(side, old, new, text):
        return {"side": side, "old": old, "new": new, "text": text, "tokens": []}

    return {
        "hub": {"user": "u", "sessions": [], "queue": [], "queue_state": "ready",
                "queue_error": "", "host_checked_at": ""},
        "diff:s1:full": {"session": "s1", "mode": "full", "state": "ready", "head_aligned": True,
                         "mr": {"project": "g/p", "iid": 1, "title": "T"},
                         "files": [{"path": "a.py", "additions": 1, "deletions": 1,
                                    "change_type": "modified", "old_path": None,
                                    "language": "python", "has_diff": True}]},
        "diff:s1:full:a.py": {"session": "s1", "mode": "full", "state": "ready", "path": "a.py",
                              "language": "python", "head_aligned": True,
                              "hunks": [{"old_start": 1, "old_count": 1, "new_start": 1,
                                         "new_count": 2, "heading": "f()", "gap_before": 0,
                                         "lines": [line("context", 1, 1, "def f():"),
                                                   line("added", None, 2, "    return 1")]}]},
        "rail:s1": {"session": "s1", "state": "ready", "highlights": [], "insights": []},
    }


def shell_on_a_review():
    client = RecordingClient(views())
    shell = Shell(client)
    shell.diff = DiffScreen(client, "s1")
    return shell, client


def press(shell, key):
    """Invoke the handler the binding registered for `key`, as the application would."""
    parsed = _parse_key(key)
    for binding in shell.bindings().bindings:
        if binding.keys == (parsed,):
            binding.handler(None)
            return
    raise AssertionError(f"nothing is bound to {key!r}")


async def settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_tab_moves_the_focus():
    shell, _ = shell_on_a_review()
    assert shell.diff.focus == "files"
    press(shell, "tab")
    assert shell.diff.focus == "body"


async def test_v_twice_sends_one_add_highlight():
    shell, client = shell_on_a_review()
    press(shell, "tab")                       # into the body
    press(shell, "j")                         # past the @@ row, onto the context line
    press(shell, "j")                         # onto the added line, new-side 2
    press(shell, "v")
    assert client.session_commands == []      # the first press only anchors
    press(shell, "v")
    await settle()
    assert len(client.session_commands) == 1
    session, command = client.session_commands[0]
    assert session == "s1"
    assert command == {"type": "add_highlight", "file": "a.py", "side": "new",
                       "line_range": {"start": 2, "end": 2}}


async def test_escape_cancels_a_selection_before_it_leaves_the_review():
    shell, client = shell_on_a_review()
    press(shell, "tab")
    press(shell, "j")
    press(shell, "j")
    press(shell, "v")
    assert shell.diff.anchor == 2
    press(shell, "escape")
    await settle()
    assert shell.diff.anchor is None
    assert shell.diff is not None             # the first escape cancelled, it did not go back
    assert client.session_commands == []


async def test_a_asks_about_the_highlight_in_focus():
    shell, client = shell_on_a_review()
    client.views["rail:s1"]["highlights"] = [
        {"id": "h1", "n": 1, "file": "a.py", "side": "new", "start": 2, "end": 2,
         "question": None, "status": "open", "stale": False, "comment_state": "context",
         "created_at": "", "context": {"state": "idle", "blame": [], "linked_issues": [],
                                       "error": ""}, "card": None}]
    press(shell, "tab")
    press(shell, "j")
    press(shell, "j")                         # onto new-side 2, inside the highlight
    press(shell, "a")
    await settle()
    assert client.session_commands == [("s1", {"type": "request_context", "highlight_id": "h1"})]


async def test_m_switches_mode_and_resubscribes():
    shell, client = shell_on_a_review()
    press(shell, "m")
    await settle()
    assert shell.diff.mode == "since"
    assert any("diff:s1:since" in group for group in client.watched)


async def test_keys_that_belong_to_the_hub_do_nothing_in_a_review():
    shell, client = shell_on_a_review()
    press(shell, "r")                         # hub.refresh
    press(shell, "c")                         # close a review
    await settle()
    assert client.commands == []
