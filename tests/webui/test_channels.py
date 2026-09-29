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


def _open_first(annotations, detail):
    annotations.index_rows.first.click()
    expect(detail.panel).to_be_visible()


def _commands(staged, session_id="s1"):
    """Every command the page actually sent, by type name."""
    writer = staged.writer(session_id)
    return [(type(command).__name__, command) for command, _ in writer.commands]


def test_a_subject_opens_both_channels(diff, annotations, detail, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(annotations, detail)
    expect(detail.tabs).to_have_count(2)
    expect(detail.tabs.nth(0)).to_contain_text("Claude")
    expect(detail.tabs.nth(1)).to_contain_text("Review")


def test_the_claude_channel_never_writes_to_the_merge_request(diff, annotations, detail, staged):
    """The leak this composition exists to prevent: an internal question becoming a posted comment."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(annotations, detail)
    detail.ask("why is this the only writer?")
    expect(detail.messages).to_have_count(1)     # the chat topic came back with it

    sent = _commands(staged)
    names = [name for name, _ in sent]
    assert "PostMessage" in names, names
    assert "SaveDraft" not in names, names       # nothing was prepared for the MR
    message = next(c for name, c in sent if name == "PostMessage")
    assert message.body == "why is this the only writer?"
    assert message.anchor is not None and message.anchor.kind.value == "highlight"


def test_the_review_channel_writes_a_draft_not_a_message(diff, annotations, detail, staged):
    """And the mirror: prose meant for the MR never lands in the chat."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(annotations, detail)
    detail.tab("Review").click()
    detail.save_draft("this needs a test")
    expect(annotations.index_rows.first.locator(".chip.comment")).to_have_count(1)   # the annotations topic agrees

    names = [name for name, _ in _commands(staged)]
    assert "SaveDraft" in names, names
    assert "PostMessage" not in names, names


def test_the_review_itself_is_a_subject_like_any_other(diff, annotations, detail, staged):
    """The MR row's Claude channel is the chat anchored to nothing."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.mr_row.click()
    detail.ask("what moved in this MR?")
    expect(detail.messages).to_have_count(1)

    message = next(c for name, c in _commands(staged) if name == "PostMessage")
    assert message.anchor is None


def test_the_panel_holds_one_conversation_at_a_time(diff, annotations, page, staged):
    """It subscribes the chat it has open and drops the one it left, as the diff does files."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    rows = annotations.index_rows
    rows.nth(0).click()
    page.wait_for_function("() => Object.keys(topicViews).some(s => /^chat:s1:/.test(s))")
    first = page.evaluate("Object.keys(topicViews).filter(s => /^chat:s1:/.test(s))")
    rows.nth(1).click()
    page.wait_for_function("(was) => {const now = Object.keys(topicViews).filter(s => /^chat:s1:/.test(s));"
                           "return now.length === 1 && now[0] !== was[0];}", arg=first)
    second = page.evaluate("Object.keys(topicViews).filter(s => /^chat:s1:/.test(s))")

    assert len(first) == 1 and len(second) == 1, (first, second)
    assert first != second, (first, second)


def test_closing_the_panel_drops_the_conversation_it_was_watching(diff, annotations, detail, page, staged):
    """Switching subjects cleaned up on the way in; closing had nobody to clean up after it, so the
    server kept rebuilding a chat with no reader until some unrelated render came along."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.index_rows.first.click()
    page.wait_for_function("() => Object.keys(topicViews).some(s => /^chat:s1:/.test(s))")

    detail.close()
    page.wait_for_function("() => !Object.keys(topicViews).some(s => /^chat:s1:/.test(s))")
    assert page.evaluate("Object.keys(topicViews).filter(s => /^chat:s1:/.test(s))") == []
    assert page.evaluate("wantedTopics.filter(s => /^chat:s1:/.test(s))") == []


# --- doubting what was said ---------------------------------------------------

def test_doubting_claudes_answer_records_it_against_the_subject(diff, annotations, detail, staged):
    """The claim travels as a note; the subject is what the answer will come back on."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(annotations, detail)
    detail.doubt_card()
    expect(detail.checking).to_have_count(1)     # the topic came back, so the command landed

    sent = _commands(staged)
    check = next(c for name, c in sent if name == "RequestCheck")
    assert check.subject.kind.value == "highlight" and check.subject.id == "h1"
    assert check.note == "`_legacy` is the pre-fleet queue."
    assert "PostMessage" not in [name for name, _ in sent], "a doubt is not a message"


def test_doubting_your_own_words_is_offered_too(diff, annotations, detail, staged):
    """Either side's claim can be wrong, so the control is on the message, not on the author."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(annotations, detail)
    detail.ask("the legacy queue is unused")
    expect(detail.messages).to_have_count(1)
    detail.doubt(0)
    expect(detail.checking).to_have_count(1)

    check = next(c for name, c in _commands(staged) if name == "RequestCheck")
    assert check.note == "the legacy queue is unused"


def test_a_doubt_says_it_is_being_checked_rather_than_going_quiet(diff, annotations, detail, staged):
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(annotations, detail)
    expect(detail.checking).to_have_count(0)
    detail.doubt_card()
    expect(detail.checking).to_have_text("Claude is double-checking this")


def test_the_review_as_a_whole_offers_no_per_message_doubt(diff, annotations, detail, staged):
    """An MR-wide message is anchored to nothing, and a doubt has to be recorded against something."""
    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    annotations.mr_row.click()
    expect(detail.panel).to_be_visible()
    detail.ask("anything else worth knowing?")
    expect(detail.messages).to_have_count(1)
    expect(detail.messages.first.locator(".noteacts")).to_have_count(0)


def test_the_composer_still_sends_after_a_frame_rebuilt_the_panel(diff, annotations, detail,
                                                                 staged, as_agent):
    """The composer reads the field it sends from, and the panel is rebuilt by every frame.

    Merging a rebuild reuses the input already on screen and discards the one that render built —
    so a handler holding the built one reads an element nobody can see, and the message goes
    nowhere. Silently: the box even clears, because it clears the copy. Only reachable after
    something has rebuilt the panel, which is why sending on a fresh one proves nothing.
    """
    from review_mate.session.commands import EmitCard

    staged.put(review_with_highlights("s1"))
    diff.load("s1")
    _open_first(annotations, detail)
    as_agent("s1", EmitCard(highlight_id=None, body="a card lands before you type"))

    detail.ask("does this still reach the server?")
    expect(detail.messages).to_contain_text("does this still reach the server?")
    assert "PostMessage" in [name for name, _ in _commands(staged)]
