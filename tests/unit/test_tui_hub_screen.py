"""The hub screen renders the view document and derives nothing from it."""
import pytest

pytest.importorskip("prompt_toolkit")

from review_mate.tui.app import HubScreen        # noqa: E402


class StubClient:
    def __init__(self, view):
        self.views = {"hub": view}
        self.status = "live"
        self.errors = {}
        self.last_command_error = ""


def session(sid="s1", project="g/p", iid=1, state="new", checked=False, **extra):
    return {"id": sid, "status": "active", "state": state, "host_checked": checked,
            "mr": {"host": "gitlab", "project": project, "iid": iid, "title": "T",
                   "url": "u", "author": "a"},
            "mr_state": "", "behind": False, "unresolved": 0, "pending": 0, "posted": 0, **extra}


def view(sessions=(), queue=(), queue_state="ready", **extra):
    return {"user": "reviewer", "sessions": list(sessions), "queue": list(queue),
            "queue_state": queue_state, "queue_error": "", "host_checked_at": "", **extra}


def text(screen):
    return "".join(t for _, t in screen.fragments())


def test_rows_list_open_reviews_before_the_queue():
    screen = HubScreen(StubClient(view(
        sessions=[session()],
        queue=[{"host": "gitlab", "project": "g/q", "iid": 9, "title": "waiting"}])))
    assert [r.kind for r in screen.rows()] == ["session", "queue"]


def test_a_queue_entry_already_open_is_not_listed_twice():
    screen = HubScreen(StubClient(view(
        sessions=[session(project="g/p", iid=1)],
        queue=[{"host": "gitlab", "project": "g/p", "iid": 1, "title": "same"},
               {"host": "gitlab", "project": "g/q", "iid": 9, "title": "other"}])))
    assert [r.kind for r in screen.rows()] == ["session", "queue"]
    assert screen.rows()[1].data["project"] == "g/q"


def test_an_unchecked_review_is_marked_as_such():
    unchecked = text(HubScreen(StubClient(view(sessions=[session(checked=False)]))))
    checked = text(HubScreen(StubClient(view(sessions=[session(checked=True)]))))
    assert "new?" in unchecked.replace(" ", "")
    assert "new?" not in checked.replace(" ", "")


def test_the_verdict_reaches_the_screen_verbatim():
    screen = HubScreen(StubClient(view(sessions=[session(state="discussions", unresolved=3)])))
    rendered = text(screen)
    assert "threads" in rendered and "3 unresolved" in rendered


def test_a_queue_failure_is_shown_not_hidden():
    screen = HubScreen(StubClient(view(queue_state="error", queue_error="gitlab 503")))
    assert "unavailable" in text(screen) and "gitlab 503" in text(screen)


def test_the_cursor_clamps_to_the_rows_that_exist():
    screen = HubScreen(StubClient(view(sessions=[session()])))
    screen.cursor = 99
    assert screen.selected().kind == "session" and screen.cursor == 0
    empty = HubScreen(StubClient(view()))
    assert empty.selected() is None


def test_the_screen_reads_through_to_the_client_rather_than_caching():
    client = StubClient(view())
    screen = HubScreen(client)
    assert "none" in text(screen)
    client.views["hub"] = view(sessions=[session(project="g/late")])
    assert "g/late" in text(screen)
