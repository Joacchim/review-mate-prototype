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
        "chat:s1": {"session": "s1", "state": "ready", "conversations": [],
                    "agent": {"state": "watching", "stale": False, "since": None,
                              "attached": True, "parked": False, "last_seen": None, "asks": []}},
        "chat:s1:review": {"session": "s1", "state": "ready", "kind": "review", "id": "",
                           "owed": False, "messages": []},
        "review:s1": {"session": "s1", "state": "ready", "drafts": [], "pending": 0, "posted": 0,
                      "approval": {"available": False, "checked": True, "you_approved": False,
                                   "approved_by": []},
                      "version": {"head": "abc", "watermark": None, "behind": False}},
    }


def shell_on_a_review():
    client = RecordingClient(views())
    shell = Shell(client)
    shell.diff = DiffScreen(client, "s1")
    return shell, client


def press(shell, key):
    """Invoke the handler the binding registered for `key`, as the application would.

    Filters are honoured: writing a message and navigating bind some of the same keys, and a test
    that ignored the condition would exercise whichever was registered first.
    """
    parsed = _parse_key(key)
    for binding in shell.bindings().bindings:
        if binding.keys == (parsed,) and binding.filter():
            binding.handler(None)
            return
    raise AssertionError(f"nothing is bound to {key!r} in this state")


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


# --- writing a message -------------------------------------------------------


def with_highlights(shell, client, count=2):
    """Put highlights in the rail and focus it, as a reader picking a subject would."""
    client.views["rail:s1"] = {
        "session": "s1", "state": "ready", "insights": [],
        "highlights": [{"id": f"h{n}", "n": n + 1, "file": "a.py", "side": "new",
                        "start": 1 + n, "end": 1 + n, "question": None, "status": "open",
                        "author": "browser", "context_requested": False,
                        "context_requested_at": "", "stale": False, "comment_state": "context",
                        "created_at": "", "context": {"state": "idle", "blame": [],
                                                      "linked_issues": [], "error": ""},
                        "card": None}
                       for n in range(count)]}
    shell.diff.focus = "rail"


async def test_c_opens_the_composer_and_enter_sends_what_was_written():
    shell, client = shell_on_a_review()
    press(shell, "c")
    assert shell.composing is True
    shell.compose.text = "what is this guard for?"
    press(shell, "enter")
    await settle()
    assert client.session_commands == [("s1", {"type": "post_message",
                                               "body": "what is this guard for?"})]
    assert shell.composing is False


async def test_a_message_written_on_a_highlight_is_anchored_to_it():
    shell, client = shell_on_a_review()
    with_highlights(shell, client)
    press(shell, "c")
    shell.compose.text = "does anything still read this?"
    press(shell, "enter")
    await settle()
    _, command = client.session_commands[-1]
    assert command["anchor"] == {"kind": "highlight", "id": "h0"}


async def test_escape_abandons_what_was_written():
    shell, client = shell_on_a_review()
    press(shell, "c")
    shell.compose.text = "never mind"
    press(shell, "escape")
    await settle()
    assert shell.composing is False and client.session_commands == []


async def test_an_empty_message_is_not_sent():
    shell, client = shell_on_a_review()
    press(shell, "c")
    shell.compose.text = "   "
    press(shell, "enter")
    await settle()
    assert client.session_commands == []


async def test_navigation_keys_are_text_while_writing():
    """The buffer has the keyboard: j is a letter, not a cursor move."""
    shell, client = shell_on_a_review()
    press(shell, "tab")
    before = shell.diff.focus
    press(shell, "c")
    with pytest.raises(AssertionError):
        press(shell, "j")
    assert shell.diff.focus == before


async def test_moving_the_rail_cursor_moves_the_conversation_watched():
    shell, client = shell_on_a_review()
    with_highlights(shell, client)
    assert shell.diff.conversation_scope() == "chat:s1:highlight:h0"
    press(shell, "j")
    await settle()
    assert shell.diff.conversation_scope() == "chat:s1:highlight:h1"
    assert "chat:s1:highlight:h1" in client.scopes


# --- writing a review comment ------------------------------------------------
# A message and a comment share one composer, so what these pin is that the kind decides where the
# text goes and which keys commit it — the two must not blur, because one is private and the other
# is what the merge request will read.

def _rail_on(shell, highlight):
    shell.client.views["rail:s1"] = {"session": "s1", "state": "ready",
                                     "highlights": [highlight], "insights": []}
    shell.diff.focus = "rail"


def hl(id="h1", n=1, comment_state="context"):
    return {"id": id, "n": n, "file": "a.py", "side": "new", "start": 1, "end": 1,
            "question": None, "status": "open", "stale": False,
            "comment_state": comment_state, "created_at": "", "card": None,
            "context": {"state": "idle", "blame": [], "linked_issues": [], "error": ""}}


async def test_d_opens_a_comment_on_whatever_the_rail_points_at():
    shell, _ = shell_on_a_review()
    _rail_on(shell, hl())
    press(shell, "d")
    assert shell.composing and shell.compose_kind == "draft"
    assert "comment>" in "".join(t for _, t in shell.compose_prompt())


async def test_with_nothing_selected_the_comment_is_the_mr_summary():
    shell, _ = shell_on_a_review()
    press(shell, "d")                          # the rail is unfocused, so this is MR-level
    assert "note>" in "".join(t for _, t in shell.compose_prompt())
    shell.compose.text = "reads well overall"
    assert shell.compose_command() == {"type": "save_draft", "highlight_id": None,
                                       "body": "reads well overall"}


async def test_saving_a_comment_sends_it_anchored():
    shell, client = shell_on_a_review()
    _rail_on(shell, hl())
    press(shell, "d")
    shell.compose.text = "this needs a test"
    press(shell, "c-s")
    await settle()
    assert client.session_commands == [("s1", {"type": "save_draft", "highlight_id": "h1",
                                               "body": "this needs a test"})]
    assert not shell.composing


async def test_enter_belongs_to_the_prose_while_a_comment_is_open():
    """The difference between a line and prose. Binding enter here would make the editor one line."""
    shell, _ = shell_on_a_review()
    press(shell, "d")
    with pytest.raises(AssertionError):
        press(shell, "enter")


async def test_a_message_still_sends_on_enter():
    shell, client = shell_on_a_review()
    press(shell, "c")                          # the other kind, unchanged
    shell.compose.text = "why is this the only writer?"
    press(shell, "enter")
    await settle()
    assert client.session_commands == [("s1", {"type": "post_message",
                                               "body": "why is this the only writer?"})]


async def test_reopening_a_comment_brings_back_what_was_written():
    """Saving again is a correction, not a second comment — so the editor starts from the text."""
    shell, _ = shell_on_a_review()
    _rail_on(shell, hl(comment_state="comment"))
    shell.client.views["review:s1"] = dict(
        shell.client.views["review:s1"],
        drafts=[{"id": "d1", "highlight_id": "h1", "body": "half a thought", "suggestion": None,
                 "status": "draft", "url": "", "thread_id": "", "created_at": ""}], pending=1)
    press(shell, "d")
    assert shell.compose.text == "half a thought"


async def test_x_discards_the_comment_prepared_here():
    shell, client = shell_on_a_review()
    _rail_on(shell, hl(comment_state="comment"))
    shell.client.views["review:s1"] = dict(
        shell.client.views["review:s1"],
        drafts=[{"id": "d1", "highlight_id": "h1", "body": "never mind", "suggestion": None,
                 "status": "draft", "url": "", "thread_id": "", "created_at": ""}], pending=1)
    press(shell, "x")
    await settle()
    assert client.session_commands == [("s1", {"type": "remove_draft", "highlight_id": "h1"})]


async def test_x_with_nothing_prepared_does_nothing():
    shell, client = shell_on_a_review()
    _rail_on(shell, hl())
    press(shell, "x")
    await settle()
    assert client.session_commands == []


async def test_escape_abandons_a_comment_without_saving_it():
    shell, client = shell_on_a_review()
    _rail_on(shell, hl())
    press(shell, "d")
    shell.compose.text = "half-written"
    press(shell, "escape")
    await settle()
    assert client.session_commands == []
    assert not shell.composing and shell.compose_kind == "message"
