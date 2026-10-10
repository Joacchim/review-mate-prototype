"""Naming somebody, in a browser.

A mention reaches a person only when the handle is exactly right, so the whole point of the list
is to insert a real one rather than let a reviewer type a guess. Two sources behind it that fail
differently — the people already on the change, which the session holds, and the project's members,
which the forge often refuses — so what the list does when the second is missing matters as much as
what it does when it is there.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import review_with_highlights
from webui.pages.detail import DetailPage
from webui.pages.diff import DiffPage
from webui.pages.annotations import AnnotationsPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def annotations(page) -> AnnotationsPage:
    return AnnotationsPage(page)


@pytest.fixture
def detail(page) -> DetailPage:
    return DetailPage(page)


def _with_people(session_id="s1", said=("eric", "prefer a guard")):
    from review_mate.session.state import ReviewThread, ThreadComment
    state = review_with_highlights(session_id)
    state.threads = [ReviewThread(id="t1", comments=[
        ThreadComment(id="c1", author=said[0], body=said[1])])]
    return state


def _open_draft(diff, annotations, detail, staged, state=None):
    staged.put(state if state is not None else _with_people())
    diff.load("s1")
    annotations.index_rows.first.click()
    detail.tab("Review").click()
    box = detail.panel.locator("textarea.draftbox").first
    expect(box).to_be_visible()
    return box


def _mentions(page):
    return page.locator("#mentions")


def test_an_at_sign_offers_the_people_on_the_change(diff, annotations, detail, staged, page):
    """No host read needed for these: the author and whoever has written here are handles the
    session already holds, and they are most of what a reviewer mentions."""
    box = _open_draft(diff, annotations, detail, staged)
    box.click()
    box.type("thanks @er")
    expect(_mentions(page)).to_be_visible()
    expect(_mentions(page).locator(".mrow", has_text="eric")).to_be_visible()


def test_picking_one_inserts_the_exact_handle(diff, annotations, detail, staged, page):
    """The reason the list exists. A handle that is nearly right notifies nobody and looks fine."""
    box = _open_draft(diff, annotations, detail, staged)
    box.click()
    box.type("thanks @er")
    _mentions(page).locator(".mrow", has_text="eric").click()
    assert box.input_value() == "thanks @eric "


def test_the_list_closes_without_taking_the_keys_with_it(diff, annotations, detail, staged, page):
    """Escape puts it away, and the arrow keys go back to being a reviewer moving a caret."""
    box = _open_draft(diff, annotations, detail, staged)
    box.click()
    box.type("thanks @er")
    expect(_mentions(page)).to_be_visible()
    page.keyboard.press("Escape")
    expect(_mentions(page)).to_have_count(0)


def test_a_name_nobody_here_has_says_so_rather_than_an_empty_box(diff, annotations, detail,
                                                                 staged, page):
    box = _open_draft(diff, annotations, detail, staged)
    box.click()
    box.type("thanks @zzz")
    expect(_mentions(page)).to_contain_text("nobody here by that name")


def test_an_email_does_not_open_the_list(diff, annotations, detail, staged, page):
    """`@` after a word is an address, not the start of a handle."""
    box = _open_draft(diff, annotations, detail, staged)
    box.click()
    box.type("ask eric@example.com")
    expect(_mentions(page)).to_have_count(0)


def test_typing_an_unknown_name_says_nothing_while_the_project_is_unknown(
        diff, annotations, detail, staged, page):
    """The member list is what makes a warning true, and this harness's forge does not offer one.
    Warning anyway is how a reviewer learns to ignore the note that matters."""
    box = _open_draft(diff, annotations, detail, staged)
    box.click()
    box.type("thanks @someone.else for the pointer")
    expect(page.locator(".mentionwarn")).to_have_count(0)


# --- when the forge will say who is in the project --------------------------

def _peopled(session_id="s1"):
    state = _with_people(session_id)
    state.mr.capabilities = {**(state.mr.capabilities or {}), "members": True}
    return state


# One handle that contains the person's name and one that does not. Without the second, searching
# by display name is indistinguishable from a plain substring match on the handle, and a test that
# cannot tell them apart does not hold the behaviour it claims to — which is why the pair is
# `john.doe`/"John Doe" and `jdupont`/"Jean Dupont", and not two of the first kind.
ROSTER = [{"username": "john.doe", "name": "John Doe"},
          {"username": "jdupont", "name": "Jean Dupont"}]


def test_members_are_offered_behind_the_people_on_the_change(diff, annotations, detail,
                                                             staged, stub_host, page):
    """Two groups, in that order: whoever is already here is more likely who you meant."""
    stub_host.roster = ROSTER
    box = _open_draft(diff, annotations, detail, staged, _peopled())
    box.click()
    box.type("ping @")
    expect(_mentions(page).locator(".mrow", has_text="jdupont")).to_be_visible()
    headers = _mentions(page).locator(".mhdr")
    assert headers.first.inner_text().lower() == "on this change"


def test_searching_by_the_name_people_say_inserts_the_handle(diff, annotations, detail,
                                                             staged, stub_host, page):
    """A reviewer types "Jean"; the mention has to carry `jdupont`, which their handle does not
    begin with and does not contain — the whole reason this is not a prefix match on the handle."""
    stub_host.roster = ROSTER
    box = _open_draft(diff, annotations, detail, staged, _peopled())
    box.click()
    box.type("ping @Jean")
    _mentions(page).locator(".mrow", has_text="jdupont").click()
    assert box.input_value() == "ping @jdupont "


def test_an_unknown_name_is_noted_once_the_project_is_known(diff, annotations, detail,
                                                            staged, stub_host, page):
    stub_host.roster = ROSTER
    box = _open_draft(diff, annotations, detail, staged, _peopled())
    box.click()
    box.type("thanks @someone.else ")
    expect(page.locator(".mentionwarn")).to_contain_text("someone.else is not in this project")


def test_a_known_name_is_not_noted(diff, annotations, detail, staged, stub_host, page):
    stub_host.roster = ROSTER
    box = _open_draft(diff, annotations, detail, staged, _peopled())
    box.click()
    box.type("thanks @jdupont ")
    expect(page.locator(".mentionwarn")).to_have_count(0)


def test_a_forge_that_refused_the_list_says_nothing_about_a_name(diff, annotations, detail,
                                                                 staged, stub_host, page):
    """GitHub asks for push access to name a repository's collaborators, which most reviewers do
    not have. Warning on a list that was never read would fire on every mention they write."""
    stub_host.members_fail = RuntimeError("403 Must have push access")
    box = _open_draft(diff, annotations, detail, staged, _peopled())
    box.click()
    box.type("thanks @someone.else ")
    expect(page.locator(".mentionwarn")).to_have_count(0)
    expect(_mentions(page)).to_have_count(0)
