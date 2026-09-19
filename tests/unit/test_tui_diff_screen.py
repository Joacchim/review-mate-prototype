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


def test_it_watches_the_listing_the_open_file_and_the_rail():
    screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py"), row("b.py")])}), "s1")
    assert screen.wanted() == ["diff:s1:full", "rail:s1", "diff:s1:full:a.py"]


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


def test_focus_cycles_through_the_three_panes():
    screen = screen_with()
    assert screen.focus == "files"
    screen.toggle_focus(); assert screen.focus == "body"
    screen.toggle_focus(); assert screen.focus == "rail"
    screen.toggle_focus(); assert screen.focus == "files"
