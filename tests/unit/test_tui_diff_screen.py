"""The review screen renders the diff scopes and decides nothing about them."""
import pytest

pytest.importorskip("prompt_toolkit")

from review_mate.tui.diff import DiffScreen, line_fragments        # noqa: E402


class StubClient:
    def __init__(self, views):
        self.views = views
        self.status = "live"
        self.errors = {}
        self.last_command_error = ""
        self.watched = []
        self.unwatched = []

    async def watch(self, scopes):
        self.watched.append(list(scopes))

    async def unwatch(self, scopes):
        self.unwatched.append(list(scopes))


def listing(files, **extra):
    return {"session": "s1", "mode": "full", "state": "ready",
            "mr": {"project": "g/p", "iid": 7, "title": "T"},
            "head_aligned": True, "files": files, **extra}


def row(path, additions=1, deletions=0):
    return {"path": path, "additions": additions, "deletions": deletions,
            "change_type": "modified", "old_path": None, "language": "python", "has_diff": True}


def body(lines=None, **extra):
    return {"session": "s1", "mode": "full", "state": "ready", "path": "a.py",
            "language": "python", "head_aligned": True,
            "hunks": [{"old_start": 1, "old_count": 1, "new_start": 1, "new_count": 2,
                       "heading": "def f():", "gap_before": 3,
                       "lines": lines or [
                           {"side": "context", "old": 1, "new": 1, "text": "def f():",
                            "tokens": [[0, 3, "keyword"]]},
                           {"side": "added", "old": None, "new": 2, "text": "    return 1",
                            "tokens": [[4, 6, "keyword"]]}]}], **extra}


def text_of(screen):
    return "".join(piece for _, piece in screen.fragments())


# --- token spans -------------------------------------------------------------

def test_spans_colour_only_what_they_cover():
    assert line_fragments("def f", [[0, 3, "keyword"]], "") == [("#c678dd", "def"), ("", " f")]


def test_an_unknown_kind_renders_plain():
    assert line_fragments("xy", [[0, 2, "no-such-kind"]], "") == [("", "xy")]


def test_the_base_style_survives_between_spans():
    out = line_fragments("a b", [[0, 1, "keyword"]], "bg:#111")
    assert out[0][0].endswith("#c678dd") and out[0][0].startswith("bg:#111")
    assert out[1] == ("bg:#111", " b")


def test_a_span_past_the_end_of_the_line_is_clamped():
    assert "".join(t for _, t in line_fragments("ab", [[0, 99, "keyword"]], "")) == "ab"


# --- the screen --------------------------------------------------------------

def test_the_file_scope_is_the_listing_plus_the_path():
    screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py"), row("pkg/b.py")])}), "s1")
    assert screen.listing == "diff:s1:full"
    assert screen.body_scope == "diff:s1:full:a.py"
    screen.next_file(1)
    assert screen.body_scope == "diff:s1:full:pkg/b.py"


def test_it_watches_everything_the_review_screen_shows():
    screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py"), row("b.py")])}), "s1")
    assert screen.wanted() == ["diff:s1:full", "rail:s1", "chat:s1", "review:s1", "threads:s1",
                               "access:s1", "chat:s1:review", "diff:s1:full:a.py"]


def test_the_diff_renders_with_gutters_and_markers():
    client = StubClient({"diff:s1:full": listing([row("a.py")]), "diff:s1:full:a.py": body()})
    rendered = text_of(DiffScreen(client, "s1"))
    assert "g/p!7" in rendered
    assert "⋯ 3 unchanged lines ⋯" in rendered
    assert "def f():" in rendered and "return 1" in rendered
    assert "+" in rendered


def test_added_lines_carry_their_own_background():
    client = StubClient({"diff:s1:full": listing([row("a.py")]), "diff:s1:full:a.py": body()})
    styles = {style for style, _ in DiffScreen(client, "s1").fragments()}
    assert any("bg:#1d2b1d" in style for style in styles)


def test_a_view_that_cannot_anchor_says_so():
    client = StubClient({"diff:s1:full": listing([row("a.py")], head_aligned=False)})
    assert "read-only" in text_of(DiffScreen(client, "s1"))


def test_an_unresolved_mode_shows_its_reason_not_an_empty_change():
    client = StubClient({"diff:s1:since": {"session": "s1", "mode": "since", "files": [],
                                           "state": "unavailable", "error": "", "mr": {},
                                           "head_aligned": True}})
    rendered = text_of(DiffScreen(client, "s1", mode="since"))
    assert "cannot serve" in rendered


def test_a_resolution_error_reaches_the_screen():
    client = StubClient({"diff:s1:since": {"session": "s1", "mode": "since", "files": [],
                                           "state": "error", "error": "git exploded", "mr": {},
                                           "head_aligned": True}})
    assert "git exploded" in text_of(DiffScreen(client, "s1", mode="since"))


def test_cycling_mode_changes_which_scope_is_wanted():
    screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py")])}), "s1")
    before = screen.wanted()
    screen.cycle_mode()
    assert screen.mode == "since"
    assert screen.wanted() != before and screen.wanted()[0] == "diff:s1:since"


def test_scrolling_the_body_never_goes_negative():
    client = StubClient({"diff:s1:full": listing([row("a.py")]), "diff:s1:full:a.py": body()})
    screen = DiffScreen(client, "s1")
    screen.toggle_focus()
    screen.move(-5)
    assert screen.scroll == 0


# --- highlights --------------------------------------------------------------

def rail(highlights=(), insights=()):
    return {"session": "s1", "state": "ready",
            "highlights": list(highlights), "insights": list(insights)}


def hl(n=1, file="a.py", start=1, end=2, card=None, context=None, stale=False):
    return {"id": f"h{n}", "n": n, "file": file, "side": "new", "start": start, "end": end,
            "question": None, "status": "open", "stale": stale, "comment_state": "context",
            "created_at": "", "context": context or {"state": "idle", "blame": [],
                                                      "linked_issues": [], "error": ""},
            "card": card}


def screen_with(highlights=(), **kwargs):
    client = StubClient({"diff:s1:full": listing([row("a.py")]),
                         "diff:s1:full:a.py": body(**kwargs),
                         "rail:s1": rail(highlights)})
    return DiffScreen(client, "s1")


def test_a_selection_anchors_then_asks():
    screen = screen_with()
    screen.focus = "body"
    screen.body_cursor = 3            # the added line, new-side 2
    assert screen.start_or_commit_selection() is None      # the first press only anchors
    assert screen.anchor == 2
    command = screen.start_or_commit_selection()
    assert command == {"type": "add_highlight", "file": "a.py", "side": "new",
                       "line_range": {"start": 2, "end": 2}}
    assert screen.anchor is None


def test_a_selection_spans_the_lines_moved_over():
    screen = screen_with()
    screen.focus = "body"
    screen.body_cursor = 2            # the context line, new-side 1
    screen.start_or_commit_selection()
    screen.move(1)                    # down onto new-side 2
    assert screen.selected_range() == (1, 2)
    assert screen.start_or_commit_selection()["line_range"] == {"start": 1, "end": 2}


def test_escape_abandons_a_selection():
    screen = screen_with()
    screen.focus = "body"
    screen.body_cursor = 3
    screen.start_or_commit_selection()
    screen.cancel_selection()
    assert screen.anchor is None and screen.selected_range() is None


def test_a_selection_on_a_hunk_header_asks_nothing():
    screen = screen_with()
    screen.focus = "body"
    screen.body_cursor = 1            # the @@ row, which stands for no line
    assert screen.start_or_commit_selection() is None
    assert screen.anchor is None


def test_highlighted_lines_are_marked_in_the_body():
    plain = text_of(screen_with())
    marked = text_of(screen_with([hl(start=2, end=2)]))
    assert "▌" not in plain and "▌" in marked


def test_the_rail_lists_what_was_asked_across_files():
    screen = screen_with([hl(1, "a.py", 1, 2), hl(2, "pkg/b.py", 9, 9)])
    rendered = text_of(screen)
    assert "#1" in rendered and "a.py:1-2" in rendered
    assert "#2" in rendered and "b.py:9-9" in rendered     # a file that is not the open one


def test_an_answered_highlight_shows_its_card():
    screen = screen_with([hl(card={"id": "c1", "body": "because the pool moved\nmore",
                                   "citations": [], "status": "", "created_at": ""})])
    assert "because the pool moved" in text_of(screen)


def test_the_cheap_tier_shows_before_any_card():
    screen = screen_with([hl(context={"state": "ready", "blame": [{"author": "luigi"}],
                                      "linked_issues": [], "error": ""})])
    assert "last touched by luigi" in text_of(screen)


def test_a_stale_highlight_says_so():
    assert "(stale)" in text_of(screen_with([hl(stale=True)]))


def test_asking_from_the_rail_targets_the_selected_highlight():
    screen = screen_with([hl(1), hl(2, start=5, end=5)])
    screen.focus = "rail"
    screen.rail_index = 1
    assert screen.ask_command() == {"type": "request_context", "highlight_id": "h2"}


def test_asking_from_the_body_targets_the_highlight_under_the_cursor():
    screen = screen_with([hl(1, "a.py", 2, 2)])
    screen.focus = "body"
    screen.body_cursor = 3            # new-side 2, inside the highlight
    assert screen.ask_command() == {"type": "request_context", "highlight_id": "h1"}


def test_asking_where_nothing_is_highlighted_does_nothing():
    screen = screen_with()
    screen.focus = "body"
    screen.body_cursor = 3
    assert screen.ask_command() is None


def test_the_file_pane_counts_what_was_asked_per_file():
    screen = screen_with([hl(1, "a.py"), hl(2, "a.py", 5, 5)])
    assert "2 asked" in text_of(screen)


def test_focus_cycles_through_every_pane():
    screen = screen_with()
    assert screen.focus == "files"
    screen.toggle_focus(); assert screen.focus == "body"
    screen.toggle_focus(); assert screen.focus == "rail"
    screen.toggle_focus(); assert screen.focus == "threads"
    screen.toggle_focus(); assert screen.focus == "files"


# --- the conversation --------------------------------------------------------


def chat_index(state="watching", stale=False, conversations=None):
    return {"session": "s1", "state": "ready", "conversations": conversations or [],
            "agent": {"state": state, "stale": stale, "since": None, "attached": state != "off",
                      "parked": False, "last_seen": None, "asks": []}}


def conversation(messages, owed=False, kind="review", ident=""):
    return {"session": "s1", "state": "ready", "kind": kind, "id": ident, "owed": owed,
            "messages": messages}


def message(role, body):
    return {"id": f"m-{body[:4]}", "role": role, "body": body, "created_at": ""}


def text_of(screen):
    return "".join(part for _, part in screen.fragments())


def test_the_review_conversation_is_what_the_screen_shows_by_default():
    screen = DiffScreen(StubClient({
        "diff:s1:full": listing([row("a.py")]),
        "chat:s1": chat_index(),
        "chat:s1:review": conversation([message("user", "what is this for?"),
                                        message("agent", "the fleet selector")]),
    }), "s1")
    rendered = text_of(screen)
    assert "Chat — this review" in rendered
    assert "what is this for?" in rendered and "the fleet selector" in rendered


def test_an_empty_conversation_says_how_to_start_one():
    screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py")]),
                                    "chat:s1": chat_index(),
                                    "chat:s1:review": conversation([])}), "s1")
    assert "c writes a message" in text_of(screen)


def test_the_agent_state_is_the_servers_word():
    for state, shown in (("working", "Claude is on it"), ("stalled", "nothing is listening"),
                         ("watching", "Claude is watching"), ("off", "no agent")):
        screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py")]),
                                        "chat:s1": chat_index(state)}), "s1")
        assert shown in text_of(screen)


def test_an_ask_that_has_sat_says_so():
    screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py")]),
                                    "chat:s1": chat_index("working", stale=True)}), "s1")
    assert "no answer yet" in text_of(screen)


def test_the_rail_cursor_picks_whose_conversation_is_shown():
    views = {"diff:s1:full": listing([row("a.py")]),
             "chat:s1": chat_index(),
             "chat:s1:review": conversation([message("user", "about the review")]),
             "chat:s1:highlight:h1": conversation([message("user", "about this line")],
                                                  kind="highlight", ident="h1"),
             "rail:s1": {"session": "s1", "state": "ready", "insights": [], "highlights": [
                 {"id": "h1", "n": 3, "file": "pkg/a.py", "side": "new", "start": 42, "end": 42,
                  "question": None, "status": "open", "author": "browser",
                  "context_requested": False, "context_requested_at": "", "stale": False,
                  "comment_state": "context", "created_at": "",
                  "context": {"state": "idle", "blame": [], "linked_issues": [], "error": ""},
                  "card": None}]}}
    screen = DiffScreen(StubClient(views), "s1")
    assert "about the review" in text_of(screen)
    screen.focus = "rail"
    rendered = text_of(screen)
    assert "Chat — #3 a.py:42" in rendered
    assert "about this line" in rendered and "about the review" not in rendered


# --- the discussions already on the merge request -----------------------------

def discussions(rows=(), unresolved=None):
    rows = list(rows)
    return {"session": "s1", "state": "ready", "threads": rows, "total": len(rows),
            "unresolved": sum(1 for t in rows if not t["resolved"]) if unresolved is None
            else unresolved}


def disc(id="t1", resolved=False, file="a.py", line=2, said="prefer a guard", n=1):
    return {"id": id, "resolved": resolved,
            "anchor": None if file is None else {"file": file, "side": "new", "line": line},
            "capabilities": {},
            "comments": [{"id": str(i), "author": "eric", "body": said, "created_at": "",
                          "mine": False} for i in range(n)]}


def screen_with_threads(rows=()):
    client = StubClient({"diff:s1:full": listing([row("a.py")]),
                         "diff:s1:full:a.py": body(),
                         "rail:s1": rail(),
                         "threads:s1": discussions(rows)})
    return DiffScreen(client, "s1")


def test_the_discussions_pane_lists_what_is_open():
    rendered = text_of(screen_with_threads([disc(), disc(id="t2", resolved=True)]))
    assert "Discussions · 1 open of 2" in rendered
    assert "prefer a guard" in rendered


def test_the_filter_starts_on_what_is_still_open():
    screen = screen_with_threads([disc(), disc(id="t2", resolved=True, said="settled")])
    assert [t["id"] for t in screen.thread_rows()] == ["t1"]
    screen.cycle_thread_filter()
    assert [t["id"] for t in screen.thread_rows()] == ["t1", "t2"]
    assert "settled" in text_of(screen)


def test_a_discussion_about_the_whole_change_says_so():
    assert "whole MR" in text_of(screen_with_threads([disc(file=None)]))


def test_pointing_at_a_discussion_makes_it_the_subject():
    """A discussion is a conversation subject, so the pane picks one the way the rail does."""
    screen = screen_with_threads([disc()])
    screen.focus = "threads"
    assert screen.subject() == {"kind": "thread", "id": "t1"}
    assert screen.conversation_scope() == "chat:s1:thread:t1"


def test_going_to_a_discussion_opens_its_line():
    screen = screen_with_threads([disc(line=2)])
    screen.focus = "threads"
    assert screen.jump_to_thread() is True
    assert screen.focus == "body"
    assert screen.body_rows()[screen.body_cursor]["line"] == 2


def test_a_line_this_view_does_not_render_lands_at_the_top():
    """Anchored to a line the mode does not show — better than landing somewhere arbitrary."""
    screen = screen_with_threads([disc(line=999)])
    screen.focus = "threads"
    assert screen.jump_to_thread() is True
    assert screen.body_cursor == 0


def test_a_discussion_with_nowhere_to_go_says_so_rather_than_guessing():
    screen = screen_with_threads([disc(file=None)])
    screen.focus = "threads"
    assert screen.jump_to_thread() is False
    assert screen.focus == "threads"      # the cursor stays where it was


# --- browsing beyond the change -----------------------------------------------

def repo(paths=(), state="ready"):
    return {"session": "s1", "state": state, "sha": "abc", "paths": list(paths), "error": ""}


def blob(lines=(), state="ready"):
    return {"session": "s1", "mode": "full", "path": "README.md", "sha": "abc",
            "language": "markdown", "state": state, "error": "",
            "lines": [{"n": i + 1, "text": t, "tokens": []} for i, t in enumerate(lines)]}


def browsing_screen(paths=("a.py", "README.md"), **views):
    client = StubClient({"diff:s1:full": listing([row("a.py")]),
                         "diff:s1:full:a.py": body(),
                         "rail:s1": rail(),
                         "tree:s1": repo(paths), **views})
    screen = DiffScreen(client, "s1")
    return screen


def test_the_file_list_shows_the_change_until_asked_for_the_repository():
    screen = browsing_screen()
    assert [r["path"] for r in screen.browse_rows()] == ["a.py"]
    screen.toggle_browse()
    assert [r["path"] for r in screen.browse_rows()] == ["a.py", "README.md"]


def test_a_changed_file_is_not_listed_twice():
    """It is in the change and in the repository; the reviewer is picking a file, not a kind."""
    screen = browsing_screen(paths=("a.py", "README.md"))
    screen.toggle_browse()
    assert [r["path"] for r in screen.browse_rows()].count("a.py") == 1


def test_browsing_subscribes_the_repository_and_stops_when_it_closes():
    screen = browsing_screen()
    assert "tree:s1" not in screen.wanted()
    screen.toggle_browse()
    assert "tree:s1" in screen.wanted()
    screen.toggle_browse()
    assert "tree:s1" not in screen.wanted()


def test_opening_a_repository_file_reads_it_from_its_blob():
    screen = browsing_screen(**{"blob:s1:full:README.md": blob(["# title", "prose"])})
    screen.toggle_browse()
    screen.file_index = 1                      # README.md
    assert screen.open_current() is True
    assert screen.blob_scope == "blob:s1:full:README.md"
    assert screen.body_scope is None           # it is not part of the change, so it has no diff
    rendered = "".join(t for _, t in screen.fragments())
    assert "# title" in rendered and "prose" in rendered


def test_opening_a_changed_file_stays_a_diff():
    screen = browsing_screen()
    screen.toggle_browse()
    screen.file_index = 0                      # a.py, which the change touched
    assert screen.open_current() is False
    assert screen.viewing is None and screen.body_scope == "diff:s1:full:a.py"


def test_a_repository_still_being_read_says_so():
    screen = browsing_screen()
    screen.client.views["tree:s1"] = repo(state="loading")
    screen.toggle_browse()
    assert "reading the repository" in "".join(t for _, t in screen.fragments())


def test_a_host_that_cannot_list_a_repository_says_so():
    screen = browsing_screen()
    screen.client.views["tree:s1"] = repo(state="unavailable")
    screen.toggle_browse()
    assert "cannot list the repository" in "".join(t for _, t in screen.fragments())


def test_closing_the_browser_closes_the_file_it_opened():
    screen = browsing_screen(**{"blob:s1:full:README.md": blob(["# title"])})
    screen.toggle_browse()
    screen.file_index = 1
    screen.open_current()
    screen.toggle_browse()
    assert screen.viewing is None              # it has nowhere to be listed any more
