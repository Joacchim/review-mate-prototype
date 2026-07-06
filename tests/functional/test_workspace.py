"""Functional tests for WorkspaceManager — real git, temp repos as the 'remote'."""
import subprocess
from pathlib import Path

import pytest

from review_mate.seams import RepoRef, CheckoutHandle, Workspace
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
    """The branch was rebased onto a moved target: since_diff replays the current work onto the
    reviewed base, so only the author's genuinely-new change shows — the target-branch change does not.
    The target change sits far from the author's edit so the replay applies without conflict."""
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
    assert res["clean"] is False             # the replay onto the old base conflicted
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
