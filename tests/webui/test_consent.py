"""Cross-repo consent in a browser: what Claude asked to read, and the reviewer's answer.

Nothing is read until this is answered, so what matters is that the ask is visible with its reason,
that both answers are reachable, and that answering removes it from what is waiting. The list is
the server's — the page used to filter the session's own state to find it.
"""
import pytest
from playwright.sync_api import expect

from review_mate.session.state import AccessRequest, AccessStatus

from webui.fixtures.scenarios import two_file_review
from webui.pages.consent import ConsentPage
from webui.pages.diff import DiffPage

REPO = "platform/virtu/vmdesc"


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def consent(page) -> ConsentPage:
    return ConsentPage(page)


def _asked(*repos, status=AccessStatus.PENDING):
    state = two_file_review("s1")
    state.access_requests = [
        AccessRequest(id=f"r{i}", repo=r, reason="it defines the type this calls", status=status)
        for i, r in enumerate(repos)]
    return state


def test_a_review_nobody_has_asked_about_shows_nothing_waiting(diff, consent, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(consent.requests).to_have_count(0)


def test_an_ask_shows_the_repository_and_why(diff, consent, staged):
    staged.put(_asked(REPO))
    diff.load("s1")
    expect(consent.request(REPO)).to_be_visible()
    expect(consent.reason(REPO)).to_contain_text("it defines the type this calls")


def test_allowing_it_takes_it_off_what_is_waiting(diff, consent, staged):
    staged.put(_asked(REPO))
    diff.load("s1")
    consent.allow(REPO)
    expect(consent.requests).to_have_count(0)


def test_refusing_it_also_takes_it_off(diff, consent, staged):
    """Refusing is an answer. What must not happen is the ask sitting there as if unanswered."""
    staged.put(_asked(REPO))
    diff.load("s1")
    consent.refuse(REPO)
    expect(consent.requests).to_have_count(0)


def test_an_already_decided_ask_is_not_waiting(diff, consent, staged):
    staged.put(_asked(REPO, status=AccessStatus.APPROVED))
    diff.load("s1")
    expect(consent.requests).to_have_count(0)


def test_each_repository_is_answered_on_its_own(diff, consent, staged):
    staged.put(_asked(REPO, "platform/virtu/shadow-vm"))
    diff.load("s1")
    expect(consent.requests).to_have_count(2)
    consent.allow(REPO)
    expect(consent.requests).to_have_count(1)
    expect(consent.request("platform/virtu/shadow-vm")).to_be_visible()
