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
