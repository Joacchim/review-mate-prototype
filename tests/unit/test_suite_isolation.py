"""The suite runs against its own files, never the reviewer's.

`create_app` defaults its knowledge base to `~/.review-mate`, and 27 call sites across these tests
take that default. A test that submits a review writes a watermark, so one of them forgetting to
pass a root is enough to edit the reviewer's own review history — which is how this got written.

The guard is an autouse fixture in `tests/conftest.py`, invisible while it works. This is the
assertion that it does.
"""
from pathlib import Path

from review_mate.config import review_mate_home, sessions_dir


def test_the_home_every_default_resolves_through_is_not_the_reviewers():
    home = review_mate_home()
    assert home != Path.home() / ".review-mate"
    assert Path.home() not in home.parents


def test_the_paths_built_on_it_follow():
    """Pinning the home is only worth anything if what is derived from it moves too."""
    assert review_mate_home() in sessions_dir().parents


def test_a_knowledge_base_taking_the_default_writes_somewhere_harmless():
    from review_mate.kb.store import ReviewKB

    kb = ReviewKB()                      # no root: exactly what create_app does
    kb.set_watermark("gitlab", "g/p", 1, "abc")
    assert Path.home() not in kb.path.parents
    assert kb.get_watermark("gitlab", "g/p", 1) == "abc"
