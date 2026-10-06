"""The review surface in a browser.

Written against the renderer as it stands, so that swapping it for the topic-driven one is proven
equivalent rather than asserted to be. What a hunk contains is the protocol suite's job; what the
page does with it is this one's.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import (CAPACITY_BODY, markdown_review, review_with_highlights,
                                      two_file_review)
from webui.pages.diff import DiffPage


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


def test_the_file_tree_lists_the_changed_files(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.files).to_have_count(2)
    expect(diff.files.filter(has_text="capacity.py")).to_be_visible()
    expect(diff.files.filter(has_text="config.py")).to_be_visible()


def test_opening_another_file_switches_the_diff(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.table).to_contain_text("pu.fleet == LEGACY")
    diff.open_file("config.py")
    expect(diff.table).to_contain_text("TIMEOUT = 120")
    expect(diff.table).not_to_contain_text("pu.fleet == LEGACY")


def test_lines_carry_their_side_and_new_side_numbering(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.rows_of("add")).to_have_count(2)
    expect(diff.rows_of("del")).to_have_count(1)
    expect(diff.rows_of("ctx")).to_have_count(2)


def test_a_deletion_is_not_selectable(diff, staged):
    """Comments anchor to new-side coordinates, so a removed line offers nothing to anchor to."""
    staged.put(two_file_review("s1"))
    diff.load("s1")
    removed = diff.rows_of("del").first
    expect(removed.locator("td.code[data-line]")).to_have_count(0)
    expect(removed.locator("td.ln")).to_have_text("")


def test_additions_are_selectable_at_their_new_line(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.line(45)).to_contain_text("pu.fleet == LEGACY")
    expect(diff.line(46)).to_contain_text("q = self._legacy")


def test_source_is_syntax_coloured(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.token("kw").first).to_be_visible()      # def / if / return


def test_the_unshown_context_offers_to_unfold(diff, staged):
    """A band before the hunk for the 43 lines it skips, and one after it for the rest of the file."""
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.unfold_bands).to_have_count(2)
    expect(diff.unfold_bands.first).to_contain_text("43")


def test_unfolding_reveals_the_real_file_content(diff, staged, stub_host):
    stub_host.files["scheduler/capacity.py"] = CAPACITY_BODY
    staged.put(two_file_review("s1"))
    diff.load("s1")
    diff.unfold_all()                       # the leading band
    expect(diff.table).to_contain_text("# line 1")
    expect(diff.table).to_contain_text("# line 43")
    expect(diff.unfold_bands).to_have_count(1)   # the trailing one is still folded


def test_side_by_side_puts_both_versions_on_one_row(diff, staged):
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.rows.first.locator("td")).to_have_count(2)
    diff.toggle_side_by_side()
    expect(diff.rows.first.locator("td")).to_have_count(4)


def test_a_markdown_file_can_be_read_instead_of_diffed(diff, staged, stub_host):
    stub_host.files["README.md"] = "# Title\n\nsome *emphasis* here\n"
    staged.put(markdown_review("s1"))
    diff.load("s1")
    expect(diff.markdown_view).to_have_count(0)
    diff.toggle_markdown()
    expect(diff.markdown_view).to_be_visible()
    expect(diff.markdown_view.locator("em")).to_have_text("emphasis")


def test_revealed_context_arrives_already_coloured(diff, staged, stub_host):
    """Unfolded lines come from the blob topic, lexed server-side against the whole file — so a
    construct that opens above a collapsed run and closes inside it is still coloured correctly,
    which a client walking only the lines it can see could not do."""
    stub_host.files["scheduler/capacity.py"] = (
        'def head():\n    """a docstring\n'
        + "\n".join(f"    line {n}" for n in range(1, 40))
        + '\n    that closes here"""\n'
        + "    def reserve(self, pu):\n        if pu.fleet == LEGACY:\n"
        + "            q = self._legacy\n        return q.take(pu.size)\n"
    )
    staged.put(two_file_review("s1"))
    diff.load("s1")
    diff.unfold_all()
    expect(diff.table).to_contain_text("a docstring")
    # the whole run reads as one block of prose rather than dissolving after its first line
    expect(diff.token("str").or_(diff.token("cmt")).first).to_be_visible()
    revealed = diff.page.locator('table.hunk td.code[data-line="20"]')
    expect(revealed.locator("span")).to_have_count(1)


# --- the diff modes ---------------------------------------------------------

def stage_advanced_review(staged, stub_host, review_kb, workspace_clean=True):
    """A review whose watermark is an older head, so the since-last surface engages."""
    from webui.fixtures.scenarios import COMMITS, reviewed_then_advanced
    staged.put(reviewed_then_advanced("s1"))
    review_kb.set_watermark("gitlab", "platform/virtu/control-plane", 137, "reviewed-head")
    stub_host.versions = [{"head_sha": "abc123", "base_sha": "base2"},
                          {"head_sha": "reviewed-head", "base_sha": "base1"}]
    stub_host.commit_list = list(COMMITS)
    return "s1"


def test_an_advanced_mr_offers_the_since_last_view(diff, staged, stub_host, review_kb):
    stage_advanced_review(staged, stub_host, review_kb)
    diff.load("s1")
    expect(diff.version_banner).to_contain_text("Updated since your last review")


def test_since_last_shows_only_what_arrived_after_the_watermark(diff, staged, stub_host, review_kb):
    stage_advanced_review(staged, stub_host, review_kb)
    diff.load("s1")
    expect(diff.table).to_contain_text("q = self._legacy")     # the full diff
    diff.show_since_last()
    expect(diff.table).to_contain_text("added since you last looked")
    expect(diff.table).not_to_contain_text("q = self._legacy")


def test_switching_back_restores_the_full_diff(diff, staged, stub_host, review_kb):
    stage_advanced_review(staged, stub_host, review_kb)
    diff.load("s1")
    diff.show_since_last()
    expect(diff.table).to_contain_text("added since you last looked")
    diff.show_full_diff()
    expect(diff.table).to_contain_text("q = self._legacy")


def test_per_commit_review_steps_through_the_commits(diff, staged, stub_host, review_kb):
    from review_mate.session.state import ChangeType, FileEntry
    from webui.fixtures.scenarios import COMMIT_DIFF
    stage_advanced_review(staged, stub_host, review_kb)
    stub_host.commit_files = {
        "aaaa111": [FileEntry(path="first.py", change_type=ChangeType.MODIFIED, language="python",
                              hunks=[{"diff": COMMIT_DIFF}])],
    }
    diff.load("s1")
    diff.toggle_per_commit()
    expect(diff.commit_bar).to_contain_text("commit 1/2")
    expect(diff.table).to_contain_text("second")


def test_a_conflicted_replay_warns_the_reviewer(diff, staged, stub_host, review_kb, stub_workspace):
    """The reviewer must not read target-branch changes as the author's work."""
    stage_advanced_review(staged, stub_host, review_kb)
    stub_workspace.clean = False
    diff.load("s1")
    diff.show_since_last()
    expect(diff.page.locator(".sincenote")).to_contain_text("may include target-branch changes")


def test_a_clean_replay_carries_no_warning(diff, staged, stub_host, review_kb):
    stage_advanced_review(staged, stub_host, review_kb)
    diff.load("s1")
    diff.show_since_last()
    expect(diff.table).to_contain_text("added since you last looked")
    expect(diff.page.locator(".sincenote")).to_have_count(0)


def test_the_file_browser_is_read_only_while_it_is_open(diff, page, staged, stub_host):
    """The repository listing costs a host read, so it is subscribed while the browser is open and
    not otherwise — a reviewer who never opens it never pays for it."""
    staged.put(two_file_review("s1"))
    stub_host.repo_tree = ["scheduler/capacity.py", "scheduler/config.py", "README.md"]
    diff.load("s1")
    assert page.evaluate("Object.keys(topicViews).filter(s => /^tree:/.test(s))") == []

    diff.show_all_repo_files()
    page.wait_for_function("() => Object.keys(topicViews).some(s => /^tree:/.test(s))")
    expect(diff.files.filter(has_text="README.md")).to_have_count(1)

    diff.show_all_repo_files()                       # off again
    page.wait_for_function("() => !wantedTopics.some(s => /^tree:/.test(s))")



def test_a_since_view_the_forge_did_not_version_says_whose_comparison_it_is(
        diff, staged, stub_host, review_kb, page):
    """A forge with no versions of its own — GitHub — is compared against this reviewer's
    watermark instead. The diff is just as anchorable, because its new side is still the head, so
    the thing worth saying is not a warning: it is that nobody else sees this comparison and the
    forge has no record of it.
    """
    from webui.fixtures.scenarios import reviewed_then_advanced

    state = reviewed_then_advanced("s1")
    state.mr.capabilities = {"commits": True}        # no diff_versions, as GitHub reports
    staged.put(state)
    review_kb.set_watermark("gitlab", "platform/virtu/control-plane", 137, "reviewed-head")
    diff.load("s1")
    diff.show_since_last()

    note = page.locator(".sincenote.local")
    expect(note).to_be_visible()
    expect(note).to_contain_text("yours alone")
    expect(note).to_contain_text("Comments still post against the latest code")
    expect(diff.table).to_contain_text("added since you last looked")   # and it still renders


def test_a_since_view_the_forge_versioned_claims_nothing_of_the_kind(
        diff, staged, stub_host, review_kb, page):
    stage_advanced_review(staged, stub_host, review_kb)
    diff.load("s1")
    diff.show_since_last()
    expect(diff.table).to_contain_text("added since you last looked")
    expect(page.locator(".sincenote.local")).to_have_count(0)


# --- putting unfolded context back -------------------------------------------------------------
#
# `expandedGaps` only ever grew: a file opened up to read around one hunk stayed open for the rest
# of the session, and a long file stayed long. What these hold is that it comes back, and that the
# state behind it comes back with it rather than leaving a gap that is open but says it is shut.

def test_a_gap_that_has_been_opened_offers_to_close_again(diff, staged, stub_host):
    stub_host.files["scheduler/capacity.py"] = CAPACITY_BODY
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.fold_bands).to_have_count(0)        # nothing is open, so nothing offers to close
    diff.unfold_all()
    expect(diff.table).to_contain_text("# line 1")
    expect(diff.fold_bands).to_have_count(1)


def test_folding_puts_the_file_back_the_way_it_was(diff, staged, stub_host):
    stub_host.files["scheduler/capacity.py"] = CAPACITY_BODY
    staged.put(two_file_review("s1"))
    diff.load("s1")
    before = diff.unfold_bands.first.inner_text()
    diff.unfold_all()
    expect(diff.table).to_contain_text("# line 43")
    diff.fold()
    expect(diff.table).not_to_contain_text("# line 43")
    expect(diff.fold_bands).to_have_count(0)
    assert diff.unfold_bands.first.inner_text() == before   # the same band, offering the same steps
    assert diff.gaps_held() == {}                           # and nothing left holding it open


def test_the_steps_come_back_one_at_a_time_from_the_end_they_were_taken(diff, staged, stub_host):
    """⤴ takes back what ▼ revealed, ⤵ what ▲ did — each from its own end of the gap."""
    stub_host.files["scheduler/capacity.py"] = CAPACITY_BODY
    staged.put(two_file_review("s1"))
    diff.load("s1")
    diff.unfold("▼")                                  # 20 from the top
    diff.unfold("▲")                                  # 20 from the bottom
    expect(diff.table).to_contain_text("# line 1")    # the first revealed from the top
    expect(diff.table).to_contain_text("# line 43")   # ... and the last from the bottom
    assert diff.gaps_held() == {"scheduler/capacity.py": {"1": {"top": 20, "bot": 20, "all": False}}}

    diff.fold("⤴")
    expect(diff.table).not_to_contain_text("# line 1")
    expect(diff.table).to_contain_text("# line 43")   # the other end is untouched
    assert diff.gaps_held() == {"scheduler/capacity.py": {"1": {"top": 0, "bot": 20, "all": False}}}

    diff.fold("⤵")
    expect(diff.table).not_to_contain_text("# line 43")
    assert diff.gaps_held() == {}                     # the last step out leaves no entry behind


def test_folding_one_gap_leaves_the_others_alone(diff, staged, stub_host):
    stub_host.files["scheduler/capacity.py"] = CAPACITY_BODY
    staged.put(two_file_review("s1"))
    diff.load("s1")
    diff.unfold_all()                                 # the leading gap
    expect(diff.fold_bands).to_have_count(1)          # landed, before asking for the next one
    diff.unfold_all()                                 # and the trailing one
    expect(diff.fold_bands).to_have_count(2)
    diff.fold()                                       # closes the first only
    expect(diff.fold_bands).to_have_count(1)
    expect(diff.table).to_contain_text("# tail 1")    # the trailing gap is still open
    expect(diff.table).not_to_contain_text("# line 1")
