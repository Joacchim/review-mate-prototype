"""since_diff correctness against a seeded on-disk git repo, with real `git`.

A rebase must not read as author work: a pure rebase yields nothing to review, a real edit yields
the edit, and the patch-id short-circuit must reach the first answer without paying for a replay.
"""
import subprocess

from review_mate.seams import RepoRef
from review_mate.workspace.manager import WorkspaceManager


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(cwd),
                        "PATH": __import__("os").environ.get("PATH", "")})


def _rev(cwd, ref):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, check=True,
                          stdout=subprocess.PIPE).stdout.decode().strip()


def _seed_repo(root):
    """A feature branch, then the target base advances; the feature is rebased onto it (pure), then
    amended with a real edit. Every SHA we need is tagged so the bare clone holds it directly.
    A larger changed region keeps the pre/post patches similar enough for range-diff to match them."""
    src = root / "src"; src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "f.txt").write_text("a\nb\nc\nd\ne\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "base1")
    _git(src, "tag", "t_base1")
    _git(src, "checkout", "-q", "-b", "feature")
    (src / "f.txt").write_text("a\nB\nc\nD\ne\n"); _git(src, "commit", "-qam", "feat: cap B and D")
    _git(src, "tag", "t_head1")
    # the target base advances (an unrelated file) — this is the rebase noise we must ignore
    _git(src, "checkout", "-q", "main")
    (src / "other.txt").write_text("x\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "base2")
    _git(src, "tag", "t_base2")
    # pure rebase: same patch replayed on the new base
    _git(src, "checkout", "-q", "feature"); _git(src, "rebase", "-q", "main")
    _git(src, "tag", "t_pure")
    # a real edit: amend the rebased commit so range-diff matches it as the same commit, evolved
    (src / "f.txt").write_text("a\nBB\nc\nD\ne\n"); _git(src, "commit", "--amend", "-qam", "feat: cap B and D")
    _git(src, "tag", "t_edit")
    return dict(clone_url=str(src), base1=_rev(src, "t_base1"), head1=_rev(src, "t_head1"),
                base2=_rev(src, "t_base2"), head2_pure=_rev(src, "t_pure"), head2_edit=_rev(src, "t_edit"))


# --- the pure-rebase short-circuit ------------------------------------------
# since_diff's replay materializes a worktree, which costs about a second on a large repo and, for
# a pure rebase, spends it to discover that nothing changed. Comparing patch-ids answers that from
# the packfile instead. These pin down both that it engages and that it declines when it must —
# by watching for the worktree command itself, since the slow path returns the same answer.

def _recording(ws):
    """Wrap ws._git so a test can assert which git commands actually ran."""
    calls = []
    original = ws._git

    async def spy(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    ws._git = spy
    return calls


def _ran_worktree(calls):
    return any("worktree" in args for args in calls)


async def test_pure_rebase_answers_without_the_worktree_replay(tmp_path):
    s = _seed_repo(tmp_path)
    ws = WorkspaceManager(root=tmp_path / "home")
    repo = RepoRef(host="gitlab", project="g/p", clone_url=s["clone_url"])
    await ws._ensure_mirror(repo)
    calls = _recording(ws)
    res = await ws.since_diff(repo, s["base1"], s["head1"], s["base2"], s["head2_pure"])
    assert res == {"diff": "", "clean": True}
    assert not _ran_worktree(calls)      # the expensive replay never ran


async def test_a_real_edit_still_takes_the_replay(tmp_path):
    s = _seed_repo(tmp_path)
    ws = WorkspaceManager(root=tmp_path / "home")
    repo = RepoRef(host="gitlab", project="g/p", clone_url=s["clone_url"])
    await ws._ensure_mirror(repo)
    calls = _recording(ws)
    res = await ws.since_diff(repo, s["base1"], s["head1"], s["base2"], s["head2_edit"])
    assert "BB" in res["diff"] and res["clean"] is True
    assert _ran_worktree(calls)          # differing patches → the replay decides, as before


def _seed_conflict_repo(root):
    """The feature and the target touch the same line, and the rebase conflict is resolved by hand
    into something neither side wrote — so the replayed patch differs from the original."""
    src = root / "csrc"; src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "shared.txt").write_text("original\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "base")
    base1 = _rev(src, "HEAD")
    _git(src, "checkout", "-q", "-b", "feature")
    (src / "shared.txt").write_text("feature version\n"); _git(src, "commit", "-qam", "feature edit")
    head1 = _rev(src, "HEAD")
    _git(src, "checkout", "-q", "main")
    (src / "shared.txt").write_text("target version\n"); _git(src, "commit", "-qam", "target edit")
    base2 = _rev(src, "HEAD")
    _git(src, "checkout", "-q", "feature")
    subprocess.run(["git", "rebase", "--onto", base2, base1], cwd=src,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (src / "shared.txt").write_text("hand-resolved, neither side\n")
    _git(src, "add", ".")
    subprocess.run(["git", "-c", "core.editor=true", "rebase", "--continue"], cwd=src,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
                        "HOME": str(src), "PATH": __import__("os").environ.get("PATH", "")})
    return dict(clone_url=str(src), base1=base1, head1=head1, base2=base2, head2=_rev(src, "HEAD"))


async def test_a_conflict_resolved_differently_is_not_a_pure_rebase(tmp_path):
    s = _seed_conflict_repo(tmp_path)
    ws = WorkspaceManager(root=tmp_path / "home")
    repo = RepoRef(host="gitlab", project="g/c", clone_url=s["clone_url"])
    mirror = await ws._ensure_mirror(repo)
    for sha in (s["base1"], s["head1"], s["base2"], s["head2"]):
        await ws._ensure_commit(mirror, sha)
    # the resolution changed the commit's patch, so the ranges no longer match
    assert await ws._is_pure_rebase(mirror, s["base1"], s["head1"], s["base2"], s["head2"]) is False
    res = await ws.since_diff(repo, s["base1"], s["head1"], s["base2"], s["head2"])
    assert "hand-resolved" in res["diff"]     # the reviewer is shown the resolution, not "nothing"


async def test_a_merge_in_the_range_declines_the_short_circuit(tmp_path):
    src = tmp_path / "msrc"; src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "f.txt").write_text("a\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "base")
    base = _rev(src, "HEAD")
    _git(src, "checkout", "-q", "-b", "feature")
    (src / "g.txt").write_text("g\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "feat")
    _git(src, "checkout", "-q", "-b", "side")
    (src / "h.txt").write_text("h\n"); _git(src, "add", "."); _git(src, "commit", "-qm", "side")
    _git(src, "checkout", "-q", "feature")
    _git(src, "merge", "-q", "--no-ff", "side", "-m", "merge side")
    head = _rev(src, "HEAD")
    ws = WorkspaceManager(root=tmp_path / "home")
    repo = RepoRef(host="gitlab", project="g/m", clone_url=str(src))
    mirror = await ws._ensure_mirror(repo)
    await ws._ensure_commit(mirror, head)
    # patch-id has nothing to hash for a merge, so the range is not comparable — never a match
    assert await ws._patch_ids(mirror, base, head) is None
    assert await ws._is_pure_rebase(mirror, base, head, base, head) is False
