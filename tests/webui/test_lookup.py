"""Finding a merge request you cannot name, and the way through to Claude when you cannot.

Host search wants a term and Claude wants a description, and the reviewer usually has the second.
So the two are separate inputs on the same page: the search runs as they type, and the description
is theirs to write and rewrite without re-running the search or losing the answer they are reading.
"""
import pytest
from playwright.sync_api import expect

from webui.pages.hub import SearchPage

HIT = {"project": "platform/virtu/control-plane", "iid": 137, "title": "rework the retry backoff",
       "url": "https://gitlab/x/-/merge_requests/137"}
OTHER = {"project": "platform/virtu/control-plane", "iid": 92, "title": "bump the base image",
         "url": "https://gitlab/x/-/merge_requests/92"}


@pytest.fixture
def search(page, base_url) -> SearchPage:
    return SearchPage(page, base_url)


def test_a_search_that_hit_still_offers_claude(search, staged, stub_host):
    """The commonest miss of all: ten matches, none of them the one they meant."""
    stub_host.search_hits = [HIT, OTHER]
    search.look_for("retry")
    expect(search.results).to_have_count(2)
    expect(search.ask_row).to_be_visible()
    expect(search.ask_row).to_contain_text("Not the one?")


def test_a_search_that_missed_offers_claude(search, staged, stub_host):
    search.look_for("something nobody indexed")
    expect(search.results).to_have_count(0)
    expect(search.ask_row).to_contain_text("Looking for it by description?")


def test_a_search_that_failed_offers_claude_too(search, staged, stub_host):
    """The case where the agent is the only route left — and the one that used to offer nothing."""
    stub_host.search_fails = RuntimeError("GitLab 503")
    search.look_for("retry")
    expect(search.error).to_be_visible()
    expect(search.ask_row).to_contain_text("GitLab search is unavailable")


def test_the_description_starts_from_the_search_term(search, staged, stub_host):
    search.look_for("backoff")
    expect(search.described).to_have_value("backoff")


def test_asking_sends_the_description_not_the_search_term(search, staged, stub_host,
                                                          as_claude_lookup):
    """Rewriting the description must not re-run the host search, and must be what Claude is sent."""
    stub_host.search_hits = [OTHER]
    search.look_for("backoff")
    search.describe("the one that made retries jittered instead of fixed")
    search.ask()
    asked = as_claude_lookup("that is !137", [HIT])
    assert asked == "the one that made retries jittered instead of fixed"

    # wait for the answer to paint before checking the results, so this asserts in the state that
    # matters rather than resolving before Claude's candidate arrives and happening to be right
    expect(search.candidates).to_have_count(1)
    expect(search.results).to_have_count(1)     # the host search was left alone


def test_claudes_answer_and_its_candidates_are_shown(search, staged, stub_host, as_claude_lookup):
    search.look_for("backoff")
    search.ask()
    as_claude_lookup("probably !137 — it reworked the backoff", [HIT])
    expect(search.answer).to_contain_text("it reworked the backoff")
    expect(search.candidates).to_have_count(1)
    expect(search.candidates.first).to_contain_text("rework the retry backoff")


def test_a_second_description_can_be_asked_after_the_first(search, staged, stub_host,
                                                           as_claude_lookup):
    """A wrong answer is a reason to rephrase, not to start the search over."""
    search.look_for("backoff")
    search.ask()
    as_claude_lookup("no idea", [])
    expect(search.answer).to_contain_text("no idea")

    search.describe("the one touching the scheduler's queue")
    search.ask()
    asked = as_claude_lookup("that is !137", [HIT])
    assert asked == "the one touching the scheduler's queue"
    expect(search.candidates).to_have_count(1)


def test_the_link_an_agent_hands_over_actually_opens_the_review(page, base_url, staged):
    """The self-review loop is only worth having if the link works: an agent opens a session and
    gives the reviewer a URL, and a wrong parameter lands them on the hub with nothing to say so."""
    from review_mate.mcp.bridge import AgentBridge
    from webui.fixtures.scenarios import review_with_highlights

    staged.put(review_with_highlights("s1"))
    handed = AgentBridge(staged, base_url=base_url).session_url("s1")
    page.goto(handed)          # exactly the link an agent would give them
    page.wait_for_selector("#ann .hrow")
    assert page.locator("#sid").inner_text().startswith("s1")


def test_a_hub_frame_does_not_take_the_search_away(search, staged, stub_host, hub_republishes):
    """The hub republishes on a timer, and the landing page it paints owns the same area the
    results are drawn into. Rebuilding it while a search is on screen threw the results away with
    no way back — the search has no topic behind it, so nothing would restore it.

    Driven rather than waited for: the ticker fires every few seconds, so the bug reads as
    intermittent and a test that sat still would pass most of the time.
    """
    stub_host.search_hits = [HIT, OTHER]
    search.look_for("retry")
    expect(search.results).to_have_count(2)
    hub_republishes()
    expect(search.results).to_have_count(2)      # still the reviewer's answer, not the listing
    expect(search.ask_row).to_be_visible()
