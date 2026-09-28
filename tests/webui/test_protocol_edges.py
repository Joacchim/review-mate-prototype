"""Protocol behaviour a browser is the only place to observe.

These read the frames the page actually received, not the pixels — the recorder's log is what
distinguishes a page that rendered the right thing from a server that sent the right thing.
"""
import time

import pytest
from playwright.sync_api import expect

from webui.fixtures.scenarios import two_file_review
from webui.pages.diff import DiffPage
from webui.pages.annotations import AnnotationsPage

OPEN_FILE = "diff:s1:full:scheduler/capacity.py"


@pytest.fixture
def diff(page, base_url) -> DiffPage:
    return DiffPage(page, base_url)


@pytest.fixture
def annotations(page) -> AnnotationsPage:
    return AnnotationsPage(page)


def scopes_since(recorder, mark: int) -> set[str]:
    return {f.get("scope") for f in recorder.frames[mark:] if f.get("type") == "scope"}


def test_a_highlight_republishes_the_annotations_and_nothing_else(diff, annotations, staged, recorder):
    """A session event rebuilds every scope the session holds, and the tokenized file is the
    largest of them by an order of magnitude. Only what changed may reach the client."""
    staged.put(two_file_review("s1"))
    diff.load("s1")
    expect(diff.table).to_contain_text("pu.fleet == LEGACY")     # the file's frame has landed
    assert OPEN_FILE in scopes_since(recorder, 0)                # ... and it came from the stream

    mark = len(recorder.frames)
    diff.ask_about(45)
    expect(annotations.rows).to_have_count(1)

    deadline = time.time() + 2                                   # let any straggler frame arrive
    while time.time() < deadline:
        time.sleep(0.1)
    assert scopes_since(recorder, mark) == {"annotations:s1"}
