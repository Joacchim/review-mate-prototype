"""One subject, two channels, in a browser.

Every row the panel opens is discussed in two places that must never become one list: the Claude
channel is a session command only the reviewer sees, and the review channel is written back to the
host for everyone. They differ in authorship, durability and write path, so the tests that matter
here are the ones that prove a message typed in one cannot leave by the other.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import review_with_highlights
from webui.pages.detail import DetailPage
from webui.pages.diff import DiffPage
from webui.pages.rail import RailPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def rail(page) -> RailPage:
    return RailPage(page)


@pytest.fixture
def detail(page) -> DetailPage:
    return DetailPage(page)


def _open_first(rail, detail):
    rail.index_rows.first.click()
    expect(detail.panel).to_be_visible()


def _commands(staged, session_id="s1"):
    """Every command the page actually sent, by type name."""
    actor = staged.actor(session_id)
    return [(type(command).__name__, command) for command, _ in actor.commands]


def test_a_subject_opens_both_channels(diff, rail, detail, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(rail, detail)
    expect(detail.tabs).to_have_count(2)
    expect(detail.tabs.nth(0)).to_contain_text("Claude")
    expect(detail.tabs.nth(1)).to_contain_text("Review")


def test_the_claude_channel_never_writes_to_the_merge_request(diff, rail, detail, staged):
    """The leak this composition exists to prevent: an internal question becoming a posted comment."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(rail, detail)
    detail.ask("why is this the only writer?")
    expect(detail.messages).to_have_count(1)     # the conversation scope came back with it

    sent = _commands(staged)
    names = [name for name, _ in sent]
    assert "PostMessage" in names, names
    assert "SaveDraft" not in names, names       # nothing was prepared for the MR
    message = next(c for name, c in sent if name == "PostMessage")
    assert message.body == "why is this the only writer?"
    assert message.anchor is not None and message.anchor.kind.value == "highlight"


def test_the_review_channel_writes_a_draft_not_a_message(diff, rail, detail, staged):
    """And the mirror: prose meant for the MR never lands in the conversation."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(rail, detail)
    detail.tab("Review").click()
    detail.save_draft("this needs a test")
    expect(rail.index_rows.first.locator(".chip.comment")).to_have_count(1)   # the rail scope agrees

    names = [name for name, _ in _commands(staged)]
    assert "SaveDraft" in names, names
    assert "PostMessage" not in names, names


def test_the_review_itself_is_a_subject_like_any_other(diff, rail, detail, staged):
    """The MR row's Claude channel is the conversation anchored to nothing."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rail.mr_row.click()
    detail.ask("what moved in this MR?")
    expect(detail.messages).to_have_count(1)

    message = next(c for name, c in _commands(staged) if name == "PostMessage")
    assert message.anchor is None


def test_the_panel_holds_one_conversation_at_a_time(diff, rail, page, staged):
    """It subscribes the conversation it has open and drops the one it left, as the diff does files."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rows = rail.index_rows
    rows.nth(0).click()
    page.wait_for_function("() => Object.keys(scopeViews).some(s => /^chat:s1:/.test(s))")
    first = page.evaluate("Object.keys(scopeViews).filter(s => /^chat:s1:/.test(s))")
    rows.nth(1).click()
    page.wait_for_function("(was) => {const now = Object.keys(scopeViews).filter(s => /^chat:s1:/.test(s));"
                           "return now.length === 1 && now[0] !== was[0];}", arg=first)
    second = page.evaluate("Object.keys(scopeViews).filter(s => /^chat:s1:/.test(s))")

    assert len(first) == 1 and len(second) == 1, (first, second)
    assert first != second, (first, second)


def test_closing_the_panel_drops_the_conversation_it_was_watching(diff, rail, detail, page, staged):
    """Switching subjects cleaned up on the way in; closing had nobody to clean up after it, so the
    server kept rebuilding a conversation with no reader until some unrelated render came along."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rail.index_rows.first.click()
    page.wait_for_function("() => Object.keys(scopeViews).some(s => /^chat:s1:/.test(s))")

    detail.close()
    page.wait_for_function("() => !Object.keys(scopeViews).some(s => /^chat:s1:/.test(s))")
    assert page.evaluate("Object.keys(scopeViews).filter(s => /^chat:s1:/.test(s))") == []
    assert page.evaluate("wantedScopes.filter(s => /^chat:s1:/.test(s))") == []


# --- doubting what was said ---------------------------------------------------

def test_doubting_claudes_answer_records_it_against_the_subject(diff, rail, detail, staged):
    """The claim travels as a note; the subject is what the answer will come back on."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(rail, detail)
    detail.doubt_card()
    expect(detail.checking).to_have_count(1)     # the scope came back, so the command landed

    sent = _commands(staged)
    check = next(c for name, c in sent if name == "RequestCheck")
    assert check.subject.kind.value == "highlight" and check.subject.id == "h1"
    assert check.note == "`_legacy` is the pre-fleet queue."
    assert "PostMessage" not in [name for name, _ in sent], "a doubt is not a message"


def test_doubting_your_own_words_is_offered_too(diff, rail, detail, staged):
    """Either side's claim can be wrong, so the control is on the message, not on the author."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(rail, detail)
    detail.ask("the legacy queue is unused")
    expect(detail.messages).to_have_count(1)
    detail.doubt(0)
    expect(detail.checking).to_have_count(1)

    check = next(c for name, c in _commands(staged) if name == "RequestCheck")
    assert check.note == "the legacy queue is unused"


def test_a_doubt_says_it_is_being_checked_rather_than_going_quiet(diff, rail, detail, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(rail, detail)
    expect(detail.checking).to_have_count(0)
    detail.doubt_card()
    expect(detail.checking).to_have_text("Claude is double-checking this")


def test_the_review_as_a_whole_offers_no_per_message_doubt(diff, rail, detail, staged):
    """An MR-wide message is anchored to nothing, and a doubt has to be recorded against something."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rail.mr_row.click()
    expect(detail.panel).to_be_visible()
    detail.ask("anything else worth knowing?")
    expect(detail.messages).to_have_count(1)
    expect(detail.messages.first.locator(".noteacts")).to_have_count(0)
