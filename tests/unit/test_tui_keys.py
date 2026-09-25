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
                         "mr": {"project": "g/p", "iid": 1, "label": "g/p !1", "title": "T"},
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
    assert shell.compose_submission() == ("session", {"type": "save_draft", "highlight_id": None,
                                                      "body": "reads well overall"})


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


# --- sending it --------------------------------------------------------------
# Sending reaches the merge request and cannot be taken back, so the gesture is deliberate: an
# uppercase key, then a confirmation. What these pin is that nothing leaves on one keypress.

def _prepared(shell, pending=1, available=True, you_approved=False):
    shell.client.views["review:s1"] = {
        "session": "s1", "state": "ready", "pending": pending, "posted": 0,
        "drafts": [{"id": "d1", "highlight_id": None, "body": "summary", "suggestion": None,
                    "status": "draft", "url": "", "thread_id": "", "created_at": ""}] * pending,
        "approval": {"available": available, "checked": True, "you_approved": you_approved,
                     "approved_by": []},
        "version": {"head": "abc", "watermark": None, "behind": False}}


async def test_sending_asks_before_it_sends():
    shell, client = shell_on_a_review()
    _prepared(shell)
    press(shell, "S")
    await settle()
    assert client.commands == []                       # nothing has left yet
    assert shell.confirming
    assert "send 1 comment?" in "".join(t for _, t in shell.fragments())


async def test_confirming_sends_the_review():
    shell, client = shell_on_a_review()
    _prepared(shell)
    press(shell, "S")
    press(shell, "y")
    await settle()
    assert client.commands == [("review.submit", {"session": "s1", "approve": False})]


async def test_escape_calls_it_off():
    shell, client = shell_on_a_review()
    _prepared(shell)
    press(shell, "S")
    press(shell, "escape")
    await settle()
    assert client.commands == [] and not shell.confirming


async def test_y_on_its_own_sends_nothing():
    """The confirmation only answers a question that was asked."""
    shell, client = shell_on_a_review()
    _prepared(shell)
    press(shell, "y")
    await settle()
    assert client.commands == []


async def test_with_nothing_prepared_there_is_nothing_to_confirm():
    shell, _ = shell_on_a_review()
    _prepared(shell, pending=0, available=False)
    press(shell, "S")
    assert not shell.confirming


async def test_approval_travels_with_the_submission():
    shell, client = shell_on_a_review()
    _prepared(shell)
    press(shell, "A")
    assert shell.approve
    assert "approval armed" in "".join(t for _, t in shell.fragments())
    press(shell, "S")
    press(shell, "y")
    await settle()
    assert client.commands == [("review.submit", {"session": "s1", "approve": True})]
    assert not shell.approve                           # spent, not left armed for the next one


async def test_an_mr_that_cannot_be_approved_does_not_arm():
    shell, _ = shell_on_a_review()
    _prepared(shell, available=False)
    press(shell, "A")
    assert not shell.approve


async def test_approving_alone_is_worth_confirming():
    """No comments prepared, but an approval armed — that is still something to send."""
    shell, client = shell_on_a_review()
    _prepared(shell, pending=0)
    press(shell, "A")
    press(shell, "S")
    assert shell.confirming
    press(shell, "y")
    await settle()
    assert client.commands == [("review.submit", {"session": "s1", "approve": True})]


# --- answering a discussion ---------------------------------------------------
# Two things can be written about a discussion and they do not go to the same place: `c` asks
# Claude and only the reviewer sees it, `R` replies and everyone on the merge request does. The
# prompt says which before a word is typed.

def _threads_on(shell, *rows):
    shell.client.views["threads:s1"] = {
        "session": "s1", "state": "ready", "threads": list(rows), "total": len(rows),
        "unresolved": sum(1 for t in rows if not t["resolved"])}
    shell.diff.focus = "threads"


def disc(id="t1", resolved=False):
    return {"id": id, "resolved": resolved, "capabilities": {},
            "anchor": {"file": "a.py", "side": "new", "line": 2},
            "comments": [{"id": "1", "author": "eric", "body": "prefer a guard",
                          "created_at": "", "mine": False}]}


async def test_replying_reaches_the_merge_request(shell_and_client=None):
    shell, client = shell_on_a_review()
    _threads_on(shell, disc())
    press(shell, "R")
    assert shell.compose_kind == "reply"
    assert "reply>" in "".join(t for _, t in shell.compose_prompt())
    shell.compose.text = "fixed in the next push"
    press(shell, "c-s")
    await settle()
    assert client.commands == [("thread.reply", {"session": "s1", "thread": "t1",
                                                 "body": "fixed in the next push"})]
    assert client.session_commands == []          # nothing private was written


async def test_asking_claude_about_a_discussion_stays_private():
    shell, client = shell_on_a_review()
    _threads_on(shell, disc())
    press(shell, "c")
    shell.compose.text = "is this guard actually needed?"
    press(shell, "enter")
    await settle()
    assert client.commands == []                  # nothing reached the merge request
    assert client.session_commands == [
        ("s1", {"type": "post_message", "body": "is this guard actually needed?",
                "anchor": {"kind": "thread", "id": "t1"}})]


async def test_resolving_and_reopening_go_both_ways():
    shell, client = shell_on_a_review()
    _threads_on(shell, disc())
    press(shell, "V")
    await settle()
    assert client.commands == [("thread.resolve", {"session": "s1", "thread": "t1",
                                                   "resolved": True})]
    # a settled discussion is not in the default filter, so reopening one means showing it first
    _threads_on(shell, disc(resolved=True))
    press(shell, "V")
    await settle()
    assert len(client.commands) == 1, "a filtered-out discussion is not there to act on"

    press(shell, "f")
    press(shell, "V")
    await settle()
    assert client.commands[-1] == ("thread.resolve", {"session": "s1", "thread": "t1",
                                                      "resolved": False})


async def test_with_no_discussion_selected_there_is_nothing_to_answer():
    shell, client = shell_on_a_review()
    _threads_on(shell)                            # the filter leaves none
    press(shell, "R")
    press(shell, "V")
    await settle()
    assert not shell.composing and client.commands == []


async def test_a_comment_written_on_a_discussion_is_the_summary_not_a_line_comment():
    """The cursor is on a thread, so there is no line to hang a comment from. It must not hang one
    off the thread's id, which is what an anchor taken without looking would do."""
    shell, client = shell_on_a_review()
    _threads_on(shell, disc())
    press(shell, "d")
    shell.compose.text = "reads well overall"
    press(shell, "c-s")
    await settle()
    assert client.session_commands == [("s1", {"type": "save_draft", "highlight_id": None,
                                               "body": "reads well overall"})]


# --- deciding what Claude may read --------------------------------------------
# Consent blocks the agent rather than the reviewer, and granting a repository read is not undone
# by changing your mind. So the prompt is modal: while it is open nothing else answers to a key.

def _asked_for(shell, *repos, status="pending"):
    shell.client.views["access:s1"] = {
        "session": "s1", "state": "ready",
        "requests": [{"id": f"r{i}", "repo": r, "reason": "it defines the type this calls",
                      "status": status, "decided_at": None} for i, r in enumerate(repos)],
        "pending": len(repos) if status == "pending" else 0}


async def test_the_header_says_the_agent_is_waiting():
    shell, _ = shell_on_a_review()
    _asked_for(shell, "platform/virtu/vmdesc")
    assert "Claude is waiting on 1 repo" in "".join(t for _, t in shell.fragments())


async def test_allowing_a_repository_says_which():
    shell, client = shell_on_a_review()
    _asked_for(shell, "platform/virtu/vmdesc")
    press(shell, "C")
    assert "let Claude read platform/virtu/vmdesc?" in "".join(t for _, t in shell.fragments())
    press(shell, "y")
    await settle()
    assert client.session_commands == [("s1", {"type": "decide_access", "request_id": "r0",
                                               "approve": True})]


async def test_refusing_is_the_other_answer_not_the_absence_of_one():
    shell, client = shell_on_a_review()
    _asked_for(shell, "platform/virtu/vmdesc")
    press(shell, "C")
    press(shell, "n")
    await settle()
    assert client.session_commands == [("s1", {"type": "decide_access", "request_id": "r0",
                                               "approve": False})]


async def test_leaving_it_waiting_answers_nothing():
    shell, client = shell_on_a_review()
    _asked_for(shell, "platform/virtu/vmdesc")
    press(shell, "C")
    press(shell, "escape")
    await settle()
    assert client.session_commands == [] and shell.deciding is None


async def test_nothing_else_answers_while_the_prompt_is_open():
    """`n` is next-file the rest of the time. A modal answer must not also move the reviewer."""
    shell, _ = shell_on_a_review()
    _asked_for(shell, "platform/virtu/vmdesc")
    press(shell, "C")
    for key in ("j", "tab", "d", "S"):
        with pytest.raises(AssertionError):
            press(shell, key)


async def test_with_nothing_asked_there_is_nothing_to_decide():
    shell, _ = shell_on_a_review()
    _asked_for(shell)
    press(shell, "C")
    assert shell.deciding is None


# --- asking for a pass over the whole change ----------------------------------

def _pass_state(shell, **fields):
    rail = dict(shell.client.views.get("rail:s1") or
                {"session": "s1", "state": "ready", "highlights": [], "insights": []})
    rail["review_pass"] = {"requested": False, "at": "", "sha": None,
                           "stale": False, "available": True, **fields}
    shell.client.views["rail:s1"] = rail


async def test_asking_for_a_review_pass():
    shell, client = shell_on_a_review()
    _pass_state(shell)
    press(shell, "i")
    await settle()
    assert client.session_commands == [("s1", {"type": "request_insights"})]


async def test_a_pass_covering_this_code_cannot_be_asked_for_again():
    shell, client = shell_on_a_review()
    _pass_state(shell, requested=True, available=False)
    press(shell, "i")
    await settle()
    assert client.session_commands == []
    assert "Claude is reviewing the change" in "".join(t for _, t in shell.fragments())


async def test_a_pass_the_change_moved_past_says_so_and_can_be_asked_again():
    """The cue must not go quiet: the reviewer is told why, and the control comes back."""
    shell, client = shell_on_a_review()
    _pass_state(shell, requested=True, stale=True, available=True, sha="old")
    assert "about an earlier version" in "".join(t for _, t in shell.fragments())
    press(shell, "i")
    await settle()
    assert client.session_commands == [("s1", {"type": "request_insights"})]


# --- disagreeing with how Claude classified a finding --------------------------

def _with_insight(criticality="high"):
    shell, client = shell_on_a_review()
    client.views["rail:s1"] = {
        "session": "s1", "state": "ready", "highlights": [],
        "insights": [{"id": "c1", "body": "this name is confusing", "citations": [],
                      "status": "", "created_at": "",
                      "label": {"theme": "bug", "criticality": criticality, "about": "",
                                "by": "agent"}}]}
    shell.diff.focus = "rail"
    return shell, client


async def test_relabelling_takes_both_halves_before_it_sends():
    """Half a label is not one, so nothing leaves until the second choice is made."""
    shell, client = _with_insight()
    press(shell, "L")
    assert shell.labelling is not None and shell.labelling["theme"] is None

    press(shell, "7")                      # naming
    assert shell.labelling["theme"] == "naming"
    assert client.session_commands == [], "still only half a label"

    press(shell, "1")                      # low
    await settle()
    session, command = client.session_commands[-1]
    assert command == {"type": "label_card", "card_id": "c1",
                       "label": {"theme": "naming", "criticality": "low", "about": ""}}
    assert shell.labelling is None


async def test_leaving_halfway_writes_nothing():
    shell, client = _with_insight()
    press(shell, "L")
    press(shell, "7")
    press(shell, "escape")
    await settle()
    assert shell.labelling is None and client.session_commands == []


async def test_the_prompt_takes_the_keyboard_while_it_is_open():
    """`n` is next-file normally. It must not move the file list out from under the prompt."""
    shell, _client = _with_insight()
    press(shell, "n")                      # reachable before the prompt opens
    press(shell, "L")
    with pytest.raises(AssertionError, match="nothing is bound"):
        press(shell, "n")


async def test_nothing_happens_on_a_highlight_or_off_the_rail():
    """Only an insight carries a label, so only an insight offers the prompt."""
    shell, _client = shell_on_a_review()
    shell.diff.focus = "body"
    press(shell, "L")
    assert shell.labelling is None
