"""The review surface in a browser.

Written against the renderer as it stands, so that swapping it for the scope-driven one is proven
equivalent rather than asserted to be. What a hunk contains is the protocol suite's job; what the
page does with it is this one's.
"""
import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import CAPACITY_BODY, markdown_review, two_file_review
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
    """Unfolded lines come from the blob scope, lexed server-side against the whole file — so a
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
