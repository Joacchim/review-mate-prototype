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


def test_it_watches_the_listing_and_the_open_file_only():
    screen = DiffScreen(StubClient({"diff:s1:full": listing([row("a.py"), row("b.py")])}), "s1")
    assert screen.wanted() == ["diff:s1:full", "diff:s1:full:a.py"]


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
