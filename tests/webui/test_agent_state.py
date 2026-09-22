"""The header light in a browser: the word the server joined, not one the page worked out.

`chat:<sid>` carries the join of presence with what the review is still owed, because neither half
answers the reviewer's question alone. The page used to compute it from the rail and the last chat
message, which is why these scenarios matter: each is one the old derivation got wrong, so a test
that passes here cannot be passing against a client-side copy of the rule.
"""
import re

import pytest
from playwright.sync_api import expect

from review_mate.session.state import Card

from webui.fixtures.scenarios import session, two_file_review
from webui.pages.diff import DiffPage
from webui.pages.shell import ShellPage

ASKED_AT = "2026-01-01T00:10:00+00:00"


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def shell(page) -> ShellPage:
    return ShellPage(page)


def _reviewed(session_id="s1", **kwargs):
    """A loadable review, with whatever the scenario needs folded onto it."""
    state = two_file_review(session_id)
    for field, value in kwargs.items():
        setattr(state, field, value)
    return state


def test_an_insights_request_is_an_outstanding_ask(diff, shell, staged):
    """The kind the browser never counted.

    It looked at the last chat message and at escalated highlights; a request for insights on the
    change as a whole was neither, so a page still deriving this reads `off` where the server
    reads `stalled` — nothing is attached in these tests, and something is owed.
    """
    staged.put(_reviewed(insights_requested=True, insights_requested_at=ASKED_AT))
    diff.load("s1")
    expect(shell.agent).to_have_class(re.compile(r"\bstalled\b"))
    expect(shell.agent_label).to_have_text("no agent watching")


def test_an_answered_insights_request_is_owed_nothing(diff, shell, staged):
    """An MR-level card answers it, so the light falls back to `off` rather than staying lit."""
    staged.put(_reviewed(
        insights_requested=True, insights_requested_at=ASKED_AT,
        cards=[Card(id="c1", highlight_id=None, body="the reducer is the only writer")],
    ))
    diff.load("s1")
    expect(shell.agent).to_have_class(re.compile(r"\boff\b"))


def test_a_review_owing_nothing_shows_no_word(diff, shell, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(shell.agent).to_have_class(re.compile(r"\boff\b"))
    expect(shell.agent_label).to_have_text("")


def test_the_landing_page_reads_the_hub_rather_than_a_session(page, base_url, shell, staged):
    """There is no session to join anything with, so the hub answers what it owns: is anyone
    listening. It must not read as `stalled` merely because no chat index has arrived."""
    staged.put(session("s1"))
    page.goto(base_url + "/")
    expect(shell.agent).to_have_class(re.compile(r"\boff\b"))
