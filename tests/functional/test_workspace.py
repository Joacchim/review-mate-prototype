"""Functional tests for WorkspaceManager — real git, temp repos as the 'remote'."""
import shutil
import subprocess
from pathlib import Path

import pytest

from review_mate.contracts import RepoRef, CheckoutHandle, RepoUnreadable, Workspace
from review_mate.workspace.manager import WorkspaceManager


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, env=_ENV)


_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": __import__("os").environ.get("PATH", ""),
    "HOME": __import__("os").environ.get("HOME", ""),
}


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "hello.py").write_text("print('v1')\n")
    _git("add", ".", cwd=src)
    _git("commit", "-m", "v1", cwd=src)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True,
                         text=True, env=_ENV).stdout.strip()
    return src, sha


def _repo(src: Path) -> RepoRef:
    return RepoRef(host="local", project="g/p", clone_url=str(src))


def _rev(src: Path, ref: str = "HEAD") -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=src, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


@pytest.fixture
def wm(tmp_path):
    return WorkspaceManager(root=tmp_path / "home")


def test_implements_workspace_protocol(wm):  # AC-8
    assert isinstance(wm, Workspace)


async def test_materialize_checks_out_content_at_commit(wm, source_repo, tmp_path):  # AC-1,5,3
    src, sha = source_repo
    handle = await wm.materialize(_repo(src), sha)
    assert isinstance(handle, CheckoutHandle)
    assert (Path(handle.path) / "hello.py").read_text() == "print('v1')\n"
    assert handle.commit == sha
    assert Path(handle.path).resolve().is_relative_to((tmp_path / "home").resolve())  # AC-3


async def test_second_materialize_reuses_mirror(wm, source_repo):  # AC-2
    src, sha = source_repo
    await wm.materialize(_repo(src), sha)
    mirrors = list((wm.root / "mirrors").iterdir())
    await wm.materialize(_repo(src), sha)
    assert list((wm.root / "mirrors").iterdir()) == mirrors  # no new mirror


async def test_release_removes_worktree(wm, source_repo):  # AC-6
    src, sha = source_repo
    handle = await wm.materialize(_repo(src), sha)
    assert Path(handle.path).exists()
    await wm.release(handle)
    assert not Path(handle.path).exists()


async def test_unknown_commit_raises(wm, source_repo):  # AC-7
    src, _ = source_repo
    with pytest.raises(Exception):
        await wm.materialize(_repo(src), "0" * 40)


async def test_failed_clone_leaves_no_poisoned_mirror(wm, tmp_path):
    """A clone that fails must not leave an empty mirror behind — otherwise mirror.exists()
    treats the broken shell as complete forever (the diff-versions "computing…" hang)."""
    bogus = RepoRef(host="local", project="g/p", clone_url=str(tmp_path / "nope"))
    with pytest.raises(Exception):
        await wm.materialize(bogus, "0" * 40)
    mirror = wm.mirror_path(bogus)
    assert not mirror.exists()                                   # no poisoned mirror
    assert not mirror.with_name(mirror.name + ".tmp").exists()   # tmp cleaned up too


async def test_since_diff_is_a_plain_diff_when_base_unchanged(wm, tmp_path):
    """No rebase (just more commits pushed): since_diff is a plain head-to-head diff showing only
    the new work — a line added in the reviewed version is context, not re-surfaced."""
    src = tmp_path / "src"; src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "f.txt").write_text("a\n"); _git("add", ".", cwd=src); _git("commit", "-m", "base", cwd=src)
    base = _rev(src)
    (src / "f.txt").write_text("a\nb\n"); _git("commit", "-am", "add b", cwd=src)
    reviewed = _rev(src)
    (src / "f.txt").write_text("a\nb\nc\n"); _git("commit", "-am", "add c", cwd=src)
    current = _rev(src)
    res = await wm.since_diff(_repo(src), base, reviewed, base, current)
    assert res["clean"] is True
    assert "+c" in res["diff"] and "+b" not in res["diff"]   # only the new line c; b is untouched context


async def test_since_diff_excludes_rebase_noise(wm, tmp_path):
    """The branch was rebased onto a moved target: since_diff replays the reviewed work onto the
    current base, then diffs against new_head, so only the author's genuinely-new change shows — the
    target-branch change does not, and the diff's new side stays at new_head (the MR head). The target
    change sits far from the author's edit so the replay applies without conflict."""
    body = "\n".join(f"l{i}" for i in range(1, 9)) + "\n"   # l1..l8 — room between top and bottom
    src = tmp_path / "src"; src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "f.txt").write_text(body); _git("add", ".", cwd=src); _git("commit", "-m", "b0", cwd=src)
    old_base = _rev(src)
    _git("checkout", "-q", "-b", "featA", cwd=src)
    (src / "f.txt").write_text(body + "AUTHOR\n"); _git("commit", "-am", "author work", cwd=src)
    reviewed = _rev(src)
    _git("checkout", "-q", "main", cwd=src)
    moved = "L1 MOVED\n" + "\n".join(f"l{i}" for i in range(2, 9)) + "\n"   # target changes only l1
    (src / "f.txt").write_text(moved); _git("commit", "-am", "target moves", cwd=src)
    new_base = _rev(src)
    _git("checkout", "-q", "-b", "featB", cwd=src)
    (src / "f.txt").write_text(moved + "AUTHOR\nAUTHOR2\n"); _git("commit", "-am", "author work v2", cwd=src)
    current = _rev(src)
    res = await wm.since_diff(_repo(src), old_base, reviewed, new_base, current)
    assert res["clean"] is True              # the replay applied cleanly, so noise was excluded
    assert "AUTHOR2" in res["diff"]          # the author's genuinely-new line since review
    assert "L1 MOVED" not in res["diff"]     # the target-branch (rebase) change is excluded


async def test_concurrent_mirror_use_clones_once(wm, source_repo):
    """The per-repo lock serializes the bare-mirror clone: two concurrent callers (a prefetch racing
    a user toggle) must not double-clone or leave a poisoned .tmp behind."""
    import asyncio
    src, sha = source_repo   # base==head → since_diff takes the plain path (no worktree, just the mirror)
    r1, r2 = await asyncio.gather(
        wm.since_diff(_repo(src), sha, sha, sha, sha),
        wm.since_diff(_repo(src), sha, sha, sha, sha))
    assert r1["clean"] and r2["clean"]
    entries = list((wm.root / "mirrors").iterdir())
    assert len([m for m in entries if m.name.endswith(".git")]) == 1   # one mirror
    assert not [m for m in entries if m.name.endswith(".tmp")]         # no leftover partial clone


async def test_since_diff_falls_back_to_plain_on_conflict(wm, tmp_path):
    """When the base moved AND the replay conflicts (author + target edited the same lines), since_diff
    returns clean=False with the raw old_head..new_head diff — a readable normal diff, not None."""
    src = tmp_path / "src"; src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "f.txt").write_text("L1\nL2\nL3\n"); _git("add", ".", cwd=src); _git("commit", "-m", "b0", cwd=src)
    old_base = _rev(src)
    _git("checkout", "-q", "-b", "featA", cwd=src)
    (src / "f.txt").write_text("L1\nAUTHOR\nL3\n"); _git("commit", "-am", "author edits L2", cwd=src)
    reviewed = _rev(src)
    _git("checkout", "-q", "main", cwd=src)
    (src / "f.txt").write_text("L1\nTARGET\nL3\n"); _git("commit", "-am", "target edits L2", cwd=src)
    new_base = _rev(src)
    _git("checkout", "-q", "-b", "featB", cwd=src)
    (src / "f.txt").write_text("L1\nAUTHOR2\nL3\n"); _git("commit", "-am", "author edits L2 again", cwd=src)
    current = _rev(src)
    res = await wm.since_diff(_repo(src), old_base, reviewed, new_base, current)
    assert res["clean"] is False             # the replay onto the current base conflicted
    assert "AUTHOR2" in res["diff"]          # still a readable normal diff (raw old_head..new_head)


async def test_seed_clone_is_not_modified(tmp_path, source_repo):  # AC-4
    src, sha = source_repo
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(src), str(seed)], check=True,
                   capture_output=True, env=_ENV)
    before = sorted(p.name for p in seed.iterdir())
    wm = WorkspaceManager(root=tmp_path / "home", seeds={"local__g_p": str(seed)})
    handle = await wm.materialize(_repo(src), sha)
    assert (Path(handle.path) / "hello.py").exists()
    assert sorted(p.name for p in seed.iterdir()) == before  # seed untouched


# --- which of a commit's lines do not survive to the head ---------------------------------------
#
# Survival, not churn: a line some later commit rewrote and another put back is what the branch
# ends up with, so it is not superseded. The distinction matters because the two are easy to
# conflate and only one of them tells a reviewer whether the code in front of them is worth
# reading closely.

@pytest.fixture
def churn_repo(tmp_path):
    """A → three lines; B → rewrites line 2; C → puts line 2 back exactly; D → appends a line."""
    src = tmp_path / "churn"
    src.mkdir()
    _git("init", "-b", "main", cwd=src)
    f = src / "f.txt"
    for message, body in (("A", "alpha\nbravo\ncharlie\n"),
                          ("B", "alpha\nBRAVO\ncharlie\n"),
                          ("C", "alpha\nbravo\ncharlie\n"),
                          ("D", "alpha\nbravo\ncharlie\ndelta\n")):
        f.write_text(body)
        _git("add", ".", cwd=src)
        _git("commit", "-m", message, cwd=src)
    return src


async def test_a_line_a_later_commit_rewrites_is_superseded(wm, churn_repo):
    out = await wm.superseded(_repo(churn_repo), _rev(churn_repo, "HEAD~2"), _rev(churn_repo))
    assert out == {"f.txt": [{"start": 2, "end": 2, "sha": _rev(churn_repo, "HEAD~1")}]}


async def test_a_line_put_back_exactly_is_not_superseded(wm, churn_repo):
    """B rewrote line 2 and C restored it, so what A wrote is what merges. Churn, not supersession —
    and the answer must not depend on whether something unrelated changed elsewhere in the file."""
    a = _rev(churn_repo, "HEAD~3")
    assert await wm.superseded(_repo(churn_repo), a, _rev(churn_repo, "HEAD~1")) == {}
    assert await wm.superseded(_repo(churn_repo), a, _rev(churn_repo)) == {}   # D appended a line


async def test_the_tip_has_nothing_after_it(wm, churn_repo):
    head = _rev(churn_repo)
    assert await wm.superseded(_repo(churn_repo), head, head) == {}


async def test_an_insertion_supersedes_nothing(wm, tmp_path):
    """Lines added between two that both survive replace neither of them."""
    src = tmp_path / "ins"
    src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "f.txt").write_text("one\ntwo\n")
    _git("add", ".", cwd=src); _git("commit", "-m", "first", cwd=src)
    first = _rev(src)
    (src / "f.txt").write_text("one\ninserted\ntwo\n")
    _git("commit", "-am", "insert between", cwd=src)
    assert await wm.superseded(_repo(src), first, _rev(src)) == {}


async def test_a_file_deleted_later_has_all_its_lines_superseded(wm, tmp_path):
    src = tmp_path / "del"
    src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "f.txt").write_text("one\ntwo\nthree\n")
    (src / "keep.txt").write_text("kept\n")
    _git("add", ".", cwd=src); _git("commit", "-m", "first", cwd=src)
    first = _rev(src)
    (src / "f.txt").unlink()
    _git("add", "-A", cwd=src); _git("commit", "-m", "drop it", cwd=src)
    out = await wm.superseded(_repo(src), first, _rev(src))
    assert out == {"f.txt": [{"start": 1, "end": 3, "sha": _rev(src)}]}


async def test_too_many_commits_to_attribute_still_says_what_is_superseded(wm, tmp_path, monkeypatch):
    """Past the limit the walk is skipped — the marker survives, the name it would carry does not."""
    src = tmp_path / "many"
    src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "f.txt").write_text("original\n")
    _git("add", ".", cwd=src); _git("commit", "-m", "first", cwd=src)
    first = _rev(src)
    for n in range(3):
        (src / "f.txt").write_text(f"rewritten {n}\n")
        _git("commit", "-am", f"rewrite {n}", cwd=src)
    monkeypatch.setattr(WorkspaceManager, "ATTRIBUTE_LIMIT", 1)
    out = await wm.superseded(_repo(src), first, _rev(src))
    assert out == {"f.txt": [{"start": 1, "end": 1, "sha": ""}]}   # superseded, by nobody named


# --- when the repository cannot be reached at all ----------------------------------------------
#
# Found on a live review: the service had no ssh agent, so every clone-derived surface failed while
# the forge half kept working — the merge request loaded and only part of the screen was missing.
# What the reviewer saw was git's own words about a promisor remote.

@pytest.mark.parametrize("stderr", [
    "git@gitlab.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
    "Please make sure you have the correct access rights and the repository exists.",
    "fatal: could not fetch 1234abcd from promisor remote",
    "fatal: Authentication failed for 'https://example/x.git'",
    "ssh: Could not resolve hostname gitlab.example: Name or service not known",
])
def test_git_failures_that_mean_the_repository_is_out_of_reach(stderr):
    from review_mate.workspace.manager import _unreadable
    assert _unreadable(stderr)


@pytest.mark.parametrize("stderr", [
    "fatal: bad object 1234abcd",
    "error: pathspec 'nope' did not match any file(s) known to git",
    "CONFLICT (content): Merge conflict in a.py",
])
def test_git_failures_that_are_about_the_change_rather_than_the_connection(stderr):
    from review_mate.workspace.manager import _unreadable
    assert not _unreadable(stderr)


async def test_a_repository_that_cannot_be_reached_says_so_rather_than_naming_a_commit(wm, tmp_path):
    """`commit not available in mirror` blames the commit for a credential problem — and sends the
    reviewer looking at the merge request instead of at the server."""
    src = tmp_path / "gone"
    src.mkdir()
    _git("init", "-b", "main", cwd=src)
    (src / "f.txt").write_text("one\n")
    _git("add", ".", cwd=src); _git("commit", "-m", "first", cwd=src)
    repo, first = _repo(src), _rev(src)
    await wm.superseded(repo, first, first)            # clones the mirror while the source exists

    shutil.rmtree(src)                                 # the far end is now unreachable
    with pytest.raises(RepoUnreadable):
        await wm.since_diff(repo, None, first, None, "0" * 40)
